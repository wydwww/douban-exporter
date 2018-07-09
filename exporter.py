# -*- coding: utf-8 -*-
from flask import Flask, Response, request, send_file, current_app
from bs4 import BeautifulSoup
from Queue import Queue
from threading import Thread, Timer
from multiprocessing import Process, Manager
from datetime import datetime, timedelta
from functools import wraps
import urllib2
import cookielib
import xlsxwriter
import logging
import random
import json
import time
import ssl
import os
import re
import sys

app = Flask(__name__)
SHEETS_DIR = 'sheets'
TTL = 21600
MAX_CONCURRENT_TASKS = 6
BID_LEN = 11
BID_LIST_LEN = 500
BIDS = []
CUSTOM_COOKIE = False
AVG_DELAY = 2.0
cookies = cookielib.LWPCookieJar('cookies.txt')
cookies.load()
for cookie in cookies:
    if cookie.name == 'bid':
        CUSTOM_COOKIE = True
handlers = [
    urllib2.HTTPHandler(),
    urllib2.HTTPSHandler(),
    urllib2.HTTPCookieProcessor(cookies)
]
opener = urllib2.build_opener(*handlers)

def jsonp(func):
    @wraps(func)
    def decorated_function(*args, **kwargs):
        callback = request.args.get('callback', False)
        if callback:
            data = str(func(*args, **kwargs).data)
            content = str(callback) + '(' + data + ')'
            mimetype = 'application/javascript'
            return current_app.response_class(content, mimetype=mimetype)
        else:
            return func(*args, **kwargs)
    return decorated_function

@app.route('/addTask', methods=['GET'])
@jsonp
def new_task():
    username = request.args.get('username')
    category = request.args.get('category')
    subtypes = request.args.get('subtypes')
    err = parameters_check(username, category)
    if err:
        return err
    username = username.lower().strip('/ ')
    stc = state_check(username, category)
    if stc:
        return stc
    parsed_subtypes = subtypes.rstrip('_').split('_')
    if (len(parsed_subtypes) != 9):
        rv['msg'] = '参数有误'
        rv['type'] = 'error'
        res = Response(json.dumps(rv), mimetype='application/json')
        return res
    subtypes = {'/collect': parsed_subtypes[0:3], '/wish': parsed_subtypes[3:6], '/do': parsed_subtypes[6:9]}
    cache = cache_check(username, category, subtypes)
    if cache:
        return cache
    rv = {}
    with count_lock:
        current_count = current_tasks.value
    if current_count >= MAX_CONCURRENT_TASKS:
        rv['msg'] = '同时间正在导出数据的人太多了, 待会儿再来吧'
        rv['type'] = 'error'
        res = Response(json.dumps(rv), mimetype='application/json')
        return res
    if not user_exists(username):
        rv['msg'] = 'ID 不存在或服务器开小差了, 请更换 ID 或稍后再试, 提醒下是网址中的用户 ID 不是用户昵称哟'
        rv['type'] = 'error'
        res = Response(json.dumps(rv), mimetype='application/json')
        return res
    else:
        ip = request.environ.get('HTTP_X_REAL_IP', request.remote_addr)
        logging.warning('[NEW TASK] request from ' + ip + ', ' + username + ', ' + category)
        Process(target=export, args=(username, category,), kwargs={'subtypes': subtypes}).start()
        rv['msg'] = '任务开始中...'
        with locks[category]:
            states[category][username] = rv['msg']
        rv['type'] = 'info'
        res = Response(json.dumps(rv), mimetype='application/json')
        return res

@app.route('/getState', methods=['GET'])
@jsonp
def get_state():
    username = request.args.get('username')
    category = request.args.get('category')
    err = parameters_check(username, category)
    if err:
        return err
    username = username.lower().strip('/ ')
    rv = {}
    with locks[category]:
        state = states[category].get(username)
    if not state:
        rv['msg'] = 'No state for this user on this category'
        rv['type'] = 'error'
        res = Response(json.dumps(rv), mimetype='application/json')
        return res
    if state.startswith('done'):
        rv['msg'] = '任务完成'
        rv['type'] = 'done'
        rv['file_url'] = state.split(',')[-1]
        with locks[category]:
            del states[category][username]
    else:
        rv['msg'] = state
        rv['type'] = 'info'
    res = Response(json.dumps(rv), mimetype='application/json')
    return res

@app.route('/getFile', methods=['GET'])
def get_file():
    filename = request.args.get('filename', 'some_file_not_exists')
    path = os.path.join(SHEETS_DIR, filename)
    if os.path.isfile(path):
        res = send_file(path)
        res.headers.add('Content-Disposition', 'attachment; filename="' + filename + '"')
        return res
    else:
        return '导出完成已超过六小时, 文件已失效, 请尝试重新导出'

@app.route('/serverStat', methods=['GET'])
def server_stat():
    serializable_states = {}
    for category, state in states.items():
        with locks[category]:
            serializable_states[category] = state.copy()
    return Response(json.dumps(serializable_states), mimetype='application/json')

def parameters_check(username, category):
    rv = {}
    if not username:
        rv['msg'] = 'Please provide a username'
        rv['type'] = 'error'
        res = Response(json.dumps(rv), mimetype='application/json')
        return res
    if category not in ['movie', 'music', 'book', 'game']:
        rv['msg'] = 'Please provide a category'
        rv['type'] = 'error'
        res = Response(json.dumps(rv), mimetype='application/json')
        return res

def state_check(username, category):
    rv = {}
    with locks[category]:
        state = states.get(category, {}).get(username)
    if state:
        if state.startswith('done'):
            rv['msg'] = '任务完成'
            rv['type'] = 'done'
            rv['file_url'] = state.split(',')[-1]
            with locks[category]:
                del states[category][username]
        else:
            rv['msg'] = '已有同类任务进行中...'
            rv['type'] = 'info'
        res = Response(json.dumps(rv), mimetype='application/json')
        return res

def cache_check(username, category, subtypes):
    rv = {}
    prefix = username + '_' + category + '_'
    if subtypes['/collect'] is not None:
        prefix = prefix + ''.join([str(i) for i in subtypes['/collect']])
    if subtypes['/wish'] is not None:
        prefix = prefix + ''.join([str(i) for i in subtypes['/wish']])
    if subtypes['/do'] is not None:
        prefix = prefix + ''.join([str(i) for i in subtypes['/do']])
    for filename in os.listdir(SHEETS_DIR):
        if filename.startswith(prefix):
            rv['msg'] = '此 ID 六小时内已导出过, 请直接下载缓存结果'
            rv['type'] = 'done'
            rv['file_url'] = filename
            res = Response(json.dumps(rv), mimetype='application/json')
            return res

def user_exists(username):
    try:
        urlopen('https://movie.douban.com/people/' + username)
    except urllib2.HTTPError as e:
        logging.warning(str(e.code) + e.reason)
        return False
    except Exception as e:
        logging.error(str(e))
        return False
    else:
        return True

def retry(tries=3, delay=1, backoff=2):
    def deco_retry(f):
        @wraps(f)
        def f_retry(*args, **kwargs):
            mtries, mdelay = tries, delay
            while mtries > 1:
                try:
                    return f(*args, **kwargs)
                except urllib2.HTTPError, e:
                    raise e
                except (urllib2.URLError, ssl.SSLError) as e:
                    msg = "%s %s %s: %s, Retrying in %d seconds..." % (f.__name__, str(args), str(kwargs), str(e), mdelay)
                    logging.warning(msg)
                    time.sleep(mdelay)
                    mtries -= 1
                    mdelay *= backoff
            return f(*args, **kwargs)
        return f_retry
    return deco_retry

@retry(tries=3, delay=1, backoff=2)
def urlopen(url):
    req = urllib2.Request(url)
    req.add_header('Accept-Language', 'zh-CN,zh;en-US,en')
    req.add_header('Referer', 'https://www.douban.com/')
    if CUSTOM_COOKIE:
        req.add_header('User-Agent', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_13_2) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/63.0.3239.132 Safari/537.36')
    else:
        req.add_header('User-Agent', 'Googlebot')
        cookie = cookielib.Cookie(None, 'bid', random.choice(BIDS), '80', '80', '.douban.com', None, None, '/', None, False, False, None, None, None, None)
        cookies.set_cookie(cookie)
    return opener.open(req, timeout=5)

def gen_bids():
    bids = []
    for i in range(BID_LIST_LEN):
        bid = []
        for x in range(BID_LEN):
            bid.append(chr(random.randint(65, 90)))
        bids.append("".join(bid))
    return bids

def log_exception(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        rv = None
        try:
            rv = func(*args, **kwargs)
        except urllib2.HTTPError as e:
            logging.warning(func.__name__ + str(args) + str(kwargs) + str(e.code) + e.reason)
        except Exception as e:
            logging.error(func.__name__ + str(args) + str(kwargs) + str(e))
        finally:
            return rv
    return wrapper

def get_urls(username, subtype, queue, category, start=0, end=0):
    rounded_start = int(start) / 15 * 15
    try:
        if category == 'game':
            page = urlopen('https://www.douban.com/people/' + username + '/games?action=' + subtype[1:] + '&start=' + str(rounded_start))
            soup = BeautifulSoup(page, 'html.parser')
            count = soup.find('div', class_='info').find('h1').string
            count = int(count.split(u'\x28')[-1][:-1])
            count = min(count, end) if end != 0 else count
            items = soup.find_all('div', class_='common-item')
        else:
            page = urlopen('https://' + category + '.douban.com/people/' + username + subtype + '?start=' + str(rounded_start))
            soup = BeautifulSoup(page, 'html.parser')
            count = soup.find('span', class_='subject-num').string
            count = int(count.split(u'\xa0')[-1].strip())
            count = min(count, end) if end != 0 else count
            items = soup.find_all('li', class_='subject-item') if category == 'book' else soup.find_all('div', class_='item')
    except Exception as get_list_err:
        logging.error('[GET_LIST_ERROR] %s, %s, %s, %d : %s' % (username, subtype, category, rounded_start, get_list_err))
        count = rounded_start + 15 + 1
    else:
        for idx, item in enumerate(items, 1):
            if rounded_start + idx < start or rounded_start + idx > count:
                continue
            try:
                if category == 'book':
                    url = item.find('h2').find('a')
                elif category == 'game':
                    url = item.find('div', class_='title').find('a')
                else:
                    url = item.find('li', class_='title').find('a')
                rv = {'url': url.get('href'), 'username': username,
                      'category': category, 'subtype': subtype,
                      'index': rounded_start + idx, 'total': count}
                date = item.find('span', class_='date')
                if category == 'movie':
                    comment = item.find('span', class_='comment')
                elif category == 'book':
                    comment = item.find('p', class_='comment')
                elif category == 'music':
                    comment = item.find('span', class_='date').parent.next_sibling.next_sibling
                    if comment:
                        comment = comment.next_element
                elif category == 'game':
                    comment = item.find('div', class_='desc').next_sibling.next_sibling
                if date:
                    rv['date'] = date.string.split()[0] if category == 'book' else date.string
                if comment and comment.string:
                    rv['comment'] = comment.string.strip()
                if subtype in ['/collect', '/do']:
                    rated = date.previous_sibling.previous_sibling
                    if rated:
                        class_idx = 1 if category == 'game' else 0
                        num_idx = 7 if category == 'game' else 6
                        rated_num = rated['class'][class_idx][num_idx]
                        if rated_num.isdigit():
                            rv['rated'] = '%.1f' % (int(rated_num) * 2.0)
                queue.put(rv)
            except Exception as list_item_parse_err:
                logging.error('[LIST_ITEM_PARSE_ERR] %s, %s, %s, %d at start %d : %s' % (username, subtype, category, idx, start, list_item_parse_err))
                continue
    finally:
        if (rounded_start + 15) < count:
            time.sleep(random.uniform(AVG_DELAY / 2.0, AVG_DELAY))
            Thread(target=get_urls, args=(username, subtype, queue, category,), kwargs={'start': rounded_start + 15, 'end': end}).start()
        else:
            queue.close()

def add_workflow(username, category, subtype, sheet, data_range=[0, 0]):
    urls_queue = ClosableQueue()
    details_queue = ClosableQueue()
    sheet_queue = ClosableQueue(maxsize=0)

    fetchers = {'movie': get_movie_details,
                'music': get_music_details,
                'book': get_book_details,
                'game': get_game_details}
    appenders = {'/collect': sheet.append_to_collect_sheet,
                 '/wish': sheet.append_to_wish_sheet,
                 '/do': sheet.append_to_do_sheet}
    threads = [StoppableWorker(log_exception(fetchers[category]), urls_queue, details_queue),
               StoppableWorker(log_exception(appenders[subtype]), details_queue, sheet_queue)]
    for thread in threads:
        thread.start()

    start = int(data_range[0])
    end = int(data_range[1])
    get_urls(username, subtype, urls_queue, category, start, end)
    urls_queue.join()
    details_queue.close()
    details_queue.join()
    logging.info('all ' + str(sheet_queue.qsize()) + subtype + ' ' + category + ' tasks done for ' + username)
    del urls_queue
    del details_queue
    del sheet_queue

def export(username, category, subtypes={}):
    global current_tasks
    with count_lock:
        current_tasks.value += 1
    logging.warning('[NEW PROCESS ADDED, pid: %d]' % os.getpid())
    prefix = username + '_' + category + '_'
    if subtypes['/collect'] is not None:
        prefix = prefix + ''.join([str(i) for i in subtypes['/collect']])
    if subtypes['/wish'] is not None:
        prefix = prefix + ''.join([str(i) for i in subtypes['/wish']])
    if subtypes['/do'] is not None:
        prefix = prefix + ''.join([str(i) for i in subtypes['/do']])
    filename = prefix + '_' + datetime.now().strftime('%y_%m_%d_%H_%M') + '.xlsx'
    path = os.path.join(SHEETS_DIR, filename)
    sheet_types ={'movie': MovieSheet, 'music': MusicSheet, 'book': BookSheet, 'game': GameSheet}
    sheet = sheet_types[category](path)
    for subtype, data_range in subtypes.iteritems():
        if (int(data_range[2]) == 1):
            add_workflow(username, category, subtype, sheet, data_range)
    sheet.save()
    with locks[category]:
        states[category][username] = 'done,' + filename
    with count_lock:
        current_tasks.value -= 1

def clear_files():
    Timer(60.0, clear_files).start()
    for file in os.listdir(SHEETS_DIR):
        path = os.path.join(SHEETS_DIR, file)
        mtime = datetime.fromtimestamp(os.stat(path).st_ctime)
        delta = timedelta(seconds=TTL)
        if datetime.now() - mtime > delta:
            os.remove(path)

class ClosableQueue(Queue):
    SENTINEL = object()

    def __init__(self, maxsize=50):
        Queue.__init__(self, maxsize=maxsize)

    def close(self):
        self.put(self.SENTINEL)

    def __iter__(self):
        while True:
            item = self.get()
            try:
                if item is self.SENTINEL:
                    return
                yield item
            finally:
                self.task_done()

class StoppableWorker(Thread):
    def __init__(self, func, in_queue, out_queue):
        super(StoppableWorker, self).__init__()
        self.func = func
        self.in_queue = in_queue
        self.out_queue = out_queue

    def run(self):
        for item in self.in_queue:
            result = self.func(item)
            self.out_queue.put(result)

class MovieSheet(object):
    def __init__(self, name):
        self.workbook = xlsxwriter.Workbook(name, {'constant_memory': True})

        self.link_format = self.workbook.add_format({'color': 'blue', 'underline': 1})
        self.bold_format = self.workbook.add_format({'bold': True})
        self.under_4_format = self.workbook.add_format({'bg_color': '#E54D42', 'bold': True})
        self.under_6_format = self.workbook.add_format({'bg_color': '#E6A243', 'bold': True})
        self.under_8_format = self.workbook.add_format({'bg_color': '#29BB9C', 'bold': True})
        self.under_10_format = self.workbook.add_format({'bg_color': '#39CA74', 'bold': True})

        self.collect_sheet = self.workbook.add_worksheet(u'看过的电影')
        self.wish_sheet = self.workbook.add_worksheet(u'想看的电影')
        self.do_sheet = self.workbook.add_worksheet(u'在看的电视剧')

        collect_do_sheet_header = [u'片名', u'导演', u'评分', u'评分人数', u'我的评分',
                                   u'我的评语', u'标记日期', u'上映日期', u'时长', u'类型', u'imdb']
        wish_sheet_header = [u'片名', u'导演', u'评分', u'评分人数', u'标记日期',
                             u'上映日期', u'时长', u'类型', u'imdb']

        self.collect_sheet.set_column(0, 0, 30)
        self.collect_sheet.set_column(1, 1, 20)
        self.collect_sheet.set_column(5, 5, 30)
        self.collect_sheet.set_column(6, 6, 12)
        self.collect_sheet.set_column(7, 7, 22)
        self.collect_sheet.set_column(8, 8, 15)
        self.collect_sheet.set_column(9, 9, 20)

        self.do_sheet.set_column(0, 0, 30)
        self.do_sheet.set_column(1, 1, 20)
        self.do_sheet.set_column(5, 5, 30)
        self.do_sheet.set_column(6, 6, 12)
        self.do_sheet.set_column(7, 7, 22)
        self.do_sheet.set_column(8, 8, 15)
        self.do_sheet.set_column(9, 9, 20)

        self.wish_sheet.set_column(0, 0, 30)
        self.wish_sheet.set_column(1, 1, 20)
        self.wish_sheet.set_column(4, 4, 12)
        self.wish_sheet.set_column(5, 5, 22)
        self.wish_sheet.set_column(6, 6, 15)
        self.wish_sheet.set_column(7, 7, 20)

        for col, item in enumerate(collect_do_sheet_header):
            self.collect_sheet.write(0, col, item)
            self.do_sheet.write(0, col, item)

        for col, item in enumerate(wish_sheet_header):
            self.wish_sheet.write(0, col, item)

        self.collect_sheet_row = 1
        self.do_sheet_row = 1
        self.wish_sheet_row = 1

    def append_to_collect_sheet(self, movie):
        if movie:
            info = [[movie.get('title'), movie.get('url')], movie.get('directors'),
                    movie.get('rating'), movie.get('votes'),
                    movie.get('rated'), movie.get('comment'),
                    movie.get('date'), movie.get('rdate'),
                    movie.get('runtime'), movie.get('genres'),
                    movie.get('imdb')]
            for col, item in enumerate(info):
                if col == 0:
                    self.collect_sheet.write_url(self.collect_sheet_row, col, item[1], self.link_format, item[0])
                elif col == 2 or col == 4:
                    fmt = self.bold_format
                    if item and item.strip() != '':
                        if float(item) < 4.0:
                            fmt = self.under_4_format
                        elif float(item) < 6.0:
                            fmt = self.under_6_format
                        elif float(item) < 8.0:
                            fmt = self.under_8_format
                        else:
                            fmt = self.under_10_format
                    self.collect_sheet.write(self.collect_sheet_row, col, item, fmt)
                else:
                    self.collect_sheet.write(self.collect_sheet_row, col, item)
            self.collect_sheet_row += 1

    def append_to_do_sheet(self, movie):
        if movie:
            info = [[movie.get('title'), movie.get('url')], movie.get('directors'),
                    movie.get('rating'), movie.get('votes'),
                    movie.get('rated'), movie.get('comment'),
                    movie.get('date'), movie.get('rdate'),
                    movie.get('runtime'), movie.get('genres'),
                    movie.get('imdb')]
            for col, item in enumerate(info):
                if col == 0:
                    self.do_sheet.write_url(self.do_sheet_row, col, item[1], self.link_format, item[0])
                elif col == 2 or col == 4:
                    fmt = self.bold_format
                    if item and item.strip() != '':
                        if float(item) < 4.0:
                            fmt = self.under_4_format
                        elif float(item) < 6.0:
                            fmt = self.under_6_format
                        elif float(item) < 8.0:
                            fmt = self.under_8_format
                        else:
                            fmt = self.under_10_format
                    self.do_sheet.write(self.do_sheet_row, col, item, fmt)
                else:
                    self.do_sheet.write(self.do_sheet_row, col, item)
            self.do_sheet_row += 1

    def append_to_wish_sheet(self, movie):
        if movie:
            info = [[movie.get('title'), movie.get('url')], movie.get('directors'),
                    movie.get('rating'), movie.get('votes'),
                    movie.get('date'), movie.get('rdate'),
                    movie.get('runtime'), movie.get('genres'),
                    movie.get('imdb')]
            for col, item in enumerate(info):
                if col == 0:
                    self.wish_sheet.write_url(self.wish_sheet_row, col, item[1], self.link_format, item[0])
                elif col == 2:
                    fmt = self.bold_format
                    if item and item.strip() != '':
                        if float(item) < 4.0:
                            fmt = self.under_4_format
                        elif float(item) < 6.0:
                            fmt = self.under_6_format
                        elif float(item) < 8.0:
                            fmt = self.under_8_format
                        else:
                            fmt = self.under_10_format
                    self.wish_sheet.write(self.wish_sheet_row, col, item, fmt)
                else:
                    self.wish_sheet.write(self.wish_sheet_row, col, item)
            self.wish_sheet_row += 1

    def save(self):
        self.workbook.close()

def get_movie_details(data):
    subtypes = {'/collect': '看过的电影', '/wish': '想看的电影', '/do': '在看的电视剧'}
    with locks[data['category']]:
        states[data['category']][data['username']] = '正在获取' + subtypes[data['subtype']] + '信息: '\
                                            + str(data['index']) + ' / ' + str(data['total'])
    rv = data
    url = data.get('url')
    page = urlopen(url)
    soup = BeautifulSoup(page, 'html.parser')
    title = soup.find('span', attrs={'property': 'v:itemreviewed'})
    rating = soup.find('strong', class_='rating_num')
    votes = soup.find('span', attrs={'property': 'v:votes'})
    runtime = soup.find('span', attrs={'property': 'v:runtime'})
    rdate = soup.find('span', attrs={'property': 'v:initialReleaseDate'})
    directors = soup.find_all('a', attrs={'rel': 'v:directedBy'})
    genres = soup.find_all('span', attrs={'property': 'v:genre'})
    imdb = soup.find(href=re.compile("http://www.imdb.com/title/."))
    rv['title'] = title.string
    if rating:
        rv['rating'] = rating.string
    if votes:
        rv['votes'] = votes.string
    if runtime:
        rv['runtime'] = runtime.string
    if rdate:
        rv['rdate'] = rdate.string
    if directors:
        rv['directors'] = ' / '.join([director.string for director in directors])
    if genres:
        rv['genres'] = ' / '.join([genre.string for genre in genres])
    if imdb:
        rv['imdb'] = imdb.string
    logging.info(str(data['index']) + ' / ' + str(data['total']) + ' ' + title.string)
    time.sleep(random.uniform(AVG_DELAY / 2.0, AVG_DELAY * 2.0))
    return rv

class MusicSheet(object):
    def __init__(self, name):
        self.workbook = xlsxwriter.Workbook(name, {'constant_memory': True})

        self.link_format = self.workbook.add_format({'color': 'blue', 'underline': 1})
        self.bold_format = self.workbook.add_format({'bold': True})
        self.under_4_format = self.workbook.add_format({'bg_color': '#E54D42', 'bold': True})
        self.under_6_format = self.workbook.add_format({'bg_color': '#E6A243', 'bold': True})
        self.under_8_format = self.workbook.add_format({'bg_color': '#29BB9C', 'bold': True})
        self.under_10_format = self.workbook.add_format({'bg_color': '#39CA74', 'bold': True})

        self.collect_sheet = self.workbook.add_worksheet(u'听过的音乐')
        self.wish_sheet = self.workbook.add_worksheet(u'想听的音乐')
        self.do_sheet = self.workbook.add_worksheet(u'在听的音乐')

        collect_do_sheet_header = [u'专辑名', u'表演者', u'评分', u'评分人数', u'我的评分',
                                   u'我的评语', u'标记日期', u'发行日期', u'出版者', u'流派']
        wish_sheet_header = [u'专辑名', u'表演者', u'评分', u'评分人数',
                             u'标记日期', u'发行日期', u'出版者', u'流派']

        self.collect_sheet.set_column(0, 0, 25)
        self.collect_sheet.set_column(1, 1, 25)
        self.collect_sheet.set_column(5, 5, 30)
        self.collect_sheet.set_column(6, 6, 12)
        self.collect_sheet.set_column(7, 7, 12)
        self.collect_sheet.set_column(8, 8, 20)
        self.collect_sheet.set_column(9, 9, 15)

        self.do_sheet.set_column(0, 0, 25)
        self.do_sheet.set_column(1, 1, 25)
        self.do_sheet.set_column(5, 5, 30)
        self.do_sheet.set_column(6, 6, 12)
        self.do_sheet.set_column(7, 7, 12)
        self.do_sheet.set_column(8, 8, 20)
        self.do_sheet.set_column(9, 9, 15)

        self.wish_sheet.set_column(0, 0, 25)
        self.wish_sheet.set_column(1, 1, 25)
        self.wish_sheet.set_column(4, 4, 12)
        self.wish_sheet.set_column(5, 5, 12)
        self.wish_sheet.set_column(6, 6, 20)
        self.wish_sheet.set_column(7, 7, 15)

        for col, item in enumerate(collect_do_sheet_header):
            self.collect_sheet.write(0, col, item)
            self.do_sheet.write(0, col, item)

        for col, item in enumerate(wish_sheet_header):
            self.wish_sheet.write(0, col, item)

        self.collect_sheet_row = 1
        self.do_sheet_row = 1
        self.wish_sheet_row = 1

    def append_to_collect_sheet(self, music):
        if music:
            info = [[music.get('title'), music.get('url')], music.get('artists'),
                    music.get('rating'), music.get('votes'),
                    music.get('rated'), music.get('comment'),
                    music.get('date'), music.get('rdate'),
                    music.get('rlabel'), music.get('genre')]
            for col, item in enumerate(info):
                if col == 0:
                    self.collect_sheet.write_url(self.collect_sheet_row, col, item[1], self.link_format, item[0])
                elif col == 2 or col == 4:
                    fmt = self.bold_format
                    if item and item.strip() != '':
                        if float(item) < 4.0:
                            fmt = self.under_4_format
                        elif float(item) < 6.0:
                            fmt = self.under_6_format
                        elif float(item) < 8.0:
                            fmt = self.under_8_format
                        else:
                            fmt = self.under_10_format
                    self.collect_sheet.write(self.collect_sheet_row, col, item, fmt)
                else:
                    self.collect_sheet.write(self.collect_sheet_row, col, item)
            self.collect_sheet_row += 1

    def append_to_do_sheet(self, music):
        if music:
            info = [[music.get('title'), music.get('url')], music.get('artists'),
                    music.get('rating'), music.get('votes'),
                    music.get('rated'), music.get('comment'),
                    music.get('date'), music.get('rdate'),
                    music.get('rlabel'), music.get('genre')]
            for col, item in enumerate(info):
                if col == 0:
                    self.do_sheet.write_url(self.do_sheet_row, col, item[1], self.link_format, item[0])
                elif col == 2 or col == 4:
                    fmt = self.bold_format
                    if item and item.strip() != '':
                        if float(item) < 4.0:
                            fmt = self.under_4_format
                        elif float(item) < 6.0:
                            fmt = self.under_6_format
                        elif float(item) < 8.0:
                            fmt = self.under_8_format
                        else:
                            fmt = self.under_10_format
                    self.do_sheet.write(self.do_sheet_row, col, item, fmt)
                else:
                    self.do_sheet.write(self.do_sheet_row, col, item)
            self.do_sheet_row += 1

    def append_to_wish_sheet(self, music):
        if music:
            info = [[music.get('title'), music.get('url')], music.get('artists'),
                    music.get('rating'), music.get('votes'),
                    music.get('date'), music.get('rdate'),
                    music.get('rlabel'), music.get('genre')]
            for col, item in enumerate(info):
                if col == 0:
                    self.wish_sheet.write_url(self.wish_sheet_row, col, item[1], self.link_format, item[0])
                elif col == 2:
                    fmt = self.bold_format
                    if item and item.strip() != '':
                        if float(item) < 4.0:
                            fmt = self.under_4_format
                        elif float(item) < 6.0:
                            fmt = self.under_6_format
                        elif float(item) < 8.0:
                            fmt = self.under_8_format
                        else:
                            fmt = self.under_10_format
                    self.wish_sheet.write(self.wish_sheet_row, col, item, fmt)
                else:
                    self.wish_sheet.write(self.wish_sheet_row, col, item)
            self.wish_sheet_row += 1

    def save(self):
        self.workbook.close()

def get_music_details(data):
    subtypes = {'/collect': '听过的音乐', '/wish': '想听的音乐', '/do': '在听的音乐'}
    with locks[data['category']]:
        states[data['category']][data['username']] = '正在获取' + subtypes[data['subtype']] + '信息: '\
                                            + str(data['index']) + ' / ' + str(data['total'])
    rv = data
    url = data.get('url')
    page = urlopen(url)
    soup = BeautifulSoup(page, 'html.parser')
    title = soup.find('div', id='wrapper').find('h1').find('span')
    rating = soup.find('strong', class_='rating_num')
    votes = soup.find('span', attrs={'property': 'v:votes'})
    info = soup.find('div', id='info')
    rlabel = info.find(text=re.compile(ur'出版', re.UNICODE))
    rdate = info.find(text=re.compile(ur'发行时间', re.UNICODE))
    genre = info.find(text=re.compile(ur'流派', re.UNICODE))
    artists = info.find(text=re.compile(ur'表演者', re.UNICODE))
    rv['title'] = title.string
    if rating:
        rv['rating'] = rating.string
    if votes:
        rv['votes'] = votes.string
    if rlabel and rlabel.next_element:
        rv['rlabel'] = rlabel.next_element.string
    if rdate and rdate.next_element:
        rv['rdate'] = rdate.next_element.string.strip()
    if genre and genre.next_element:
        rv['genre'] = genre.next_element.string.strip()
    if artists:
        artists = artists.parent.find_all('a')
        rv['artists'] = ' / '.join([artist.string for artist in artists])
    logging.info(str(data['index']) + ' / ' + str(data['total']) + ' ' + title.string)
    time.sleep(random.uniform(AVG_DELAY / 2.0, AVG_DELAY * 2.0))
    return rv

class BookSheet(object):
    def __init__(self, name):
        self.workbook = xlsxwriter.Workbook(name, {'constant_memory': True})

        self.link_format = self.workbook.add_format({'color': 'blue', 'underline': 1})
        self.bold_format = self.workbook.add_format({'bold': True})
        self.under_4_format = self.workbook.add_format({'bg_color': '#E54D42', 'bold': True})
        self.under_6_format = self.workbook.add_format({'bg_color': '#E6A243', 'bold': True})
        self.under_8_format = self.workbook.add_format({'bg_color': '#29BB9C', 'bold': True})
        self.under_10_format = self.workbook.add_format({'bg_color': '#39CA74', 'bold': True})

        self.collect_sheet = self.workbook.add_worksheet(u'读过的书籍')
        self.wish_sheet = self.workbook.add_worksheet(u'想读的书籍')
        self.do_sheet = self.workbook.add_worksheet(u'在读的书籍')

        collect_do_sheet_header = [u'书名', u'作者', u'评分', u'评分人数', u'我的评分',
                                   u'我的评语', u'标记日期', u'出版日期', u'出版社', u'页数']
        wish_sheet_header = [u'书名', u'作者', u'评分', u'评分人数',
                             u'标记日期', u'出版日期', u'出版社', u'页数']

        self.collect_sheet.set_column(0, 0, 25)
        self.collect_sheet.set_column(1, 1, 25)
        self.collect_sheet.set_column(5, 5, 30)
        self.collect_sheet.set_column(6, 6, 12)
        self.collect_sheet.set_column(7, 7, 12)
        self.collect_sheet.set_column(8, 8, 25)
        self.collect_sheet.set_column(9, 9, 10)

        self.do_sheet.set_column(0, 0, 25)
        self.do_sheet.set_column(1, 1, 25)
        self.do_sheet.set_column(5, 5, 30)
        self.do_sheet.set_column(6, 6, 12)
        self.do_sheet.set_column(7, 7, 12)
        self.do_sheet.set_column(8, 8, 25)
        self.do_sheet.set_column(9, 9, 10)

        self.wish_sheet.set_column(0, 0, 25)
        self.wish_sheet.set_column(1, 1, 25)
        self.wish_sheet.set_column(4, 4, 12)
        self.wish_sheet.set_column(5, 5, 12)
        self.wish_sheet.set_column(6, 6, 25)
        self.wish_sheet.set_column(7, 7, 10)

        for col, item in enumerate(collect_do_sheet_header):
            self.collect_sheet.write(0, col, item)
            self.do_sheet.write(0, col, item)

        for col, item in enumerate(wish_sheet_header):
            self.wish_sheet.write(0, col, item)

        self.collect_sheet_row = 1
        self.do_sheet_row = 1
        self.wish_sheet_row = 1

    def append_to_collect_sheet(self, book):
        if book:
            info = [[book.get('title'), book.get('url')], book.get('authors'),
                    book.get('rating'), book.get('votes'),
                    book.get('rated'), book.get('comment'),
                    book.get('date'), book.get('rdate'),
                    book.get('press'), book.get('page')]
            for col, item in enumerate(info):
                if col == 0:
                    self.collect_sheet.write_url(self.collect_sheet_row, col, item[1], self.link_format, item[0])
                elif col == 2 or col == 4:
                    fmt = self.bold_format
                    if item and item.strip() != '':
                        if float(item) < 4.0:
                            fmt = self.under_4_format
                        elif float(item) < 6.0:
                            fmt = self.under_6_format
                        elif float(item) < 8.0:
                            fmt = self.under_8_format
                        else:
                            fmt = self.under_10_format
                    self.collect_sheet.write(self.collect_sheet_row, col, item, fmt)
                else:
                    self.collect_sheet.write(self.collect_sheet_row, col, item)
            self.collect_sheet_row += 1

    def append_to_do_sheet(self, book):
        if book:
            info = [[book.get('title'), book.get('url')], book.get('authors'),
                    book.get('rating'), book.get('votes'),
                    book.get('rated'), book.get('comment'),
                    book.get('date'), book.get('rdate'),
                    book.get('press'), book.get('page')]
            for col, item in enumerate(info):
                if col == 0:
                    self.do_sheet.write_url(self.do_sheet_row, col, item[1], self.link_format, item[0])
                elif col == 2 or col == 4:
                    fmt = self.bold_format
                    if item and item.strip() != '':
                        if float(item) < 4.0:
                            fmt = self.under_4_format
                        elif float(item) < 6.0:
                            fmt = self.under_6_format
                        elif float(item) < 8.0:
                            fmt = self.under_8_format
                        else:
                            fmt = self.under_10_format
                    self.do_sheet.write(self.do_sheet_row, col, item, fmt)
                else:
                    self.do_sheet.write(self.do_sheet_row, col, item)
            self.do_sheet_row += 1

    def append_to_wish_sheet(self, book):
        if book:
            info = [[book.get('title'), book.get('url')], book.get('authors'),
                    book.get('rating'), book.get('votes'),
                    book.get('date'), book.get('rdate'),
                    book.get('press'), book.get('page')]
            for col, item in enumerate(info):
                if col == 0:
                    self.wish_sheet.write_url(self.wish_sheet_row, col, item[1], self.link_format, item[0])
                elif col == 2:
                    fmt = self.bold_format
                    if item and item.strip() != '':
                        if float(item) < 4.0:
                            fmt = self.under_4_format
                        elif float(item) < 6.0:
                            fmt = self.under_6_format
                        elif float(item) < 8.0:
                            fmt = self.under_8_format
                        else:
                            fmt = self.under_10_format
                    self.wish_sheet.write(self.wish_sheet_row, col, item, fmt)
                else:
                    self.wish_sheet.write(self.wish_sheet_row, col, item)
            self.wish_sheet_row += 1

    def save(self):
        self.workbook.close()

def get_book_details(data):
    subtypes = {'/collect': '看过的书籍', '/wish': '想看的书籍', '/do': '在看的书籍'}
    with locks[data['category']]:
        states[data['category']][data['username']] = '正在获取' + subtypes[data['subtype']] + '信息: '\
                                            + str(data['index']) + ' / ' + str(data['total'])
    rv = data
    url = data.get('url')
    page = urlopen(url)
    soup = BeautifulSoup(page, 'html.parser')
    title = soup.find('span', attrs={'property': 'v:itemreviewed'})
    rating = soup.find('strong', class_='rating_num')
    votes = soup.find('span', attrs={'property': 'v:votes'})
    info = soup.find('div', id='info')
    press = info.find(text=re.compile(ur'出版社', re.UNICODE))
    rdate = info.find(text=re.compile(ur'出版年', re.UNICODE))
    page = info.find(text=re.compile(ur'页数', re.UNICODE))
    authors = info.find(text=re.compile(ur'作者', re.UNICODE))
    rv['title'] = title.string
    if rating:
        rv['rating'] = rating.string
    if votes:
        rv['votes'] = votes.string
    if press and press.next_element:
        rv['press'] = press.next_element.string.strip()
    if rdate and rdate.next_element:
        rv['rdate'] = rdate.next_element.string.strip()
    if page and page.next_element:
        rv['page'] = page.next_element.string.strip()
    if authors:
        authors = authors.parent.parent.find_all('a')
        rv['authors'] = ' / '.join([author.string for author in authors])
    logging.info(str(data['index']) + ' / ' + str(data['total']) + ' ' + title.string)
    time.sleep(random.uniform(AVG_DELAY / 2.0, AVG_DELAY * 2.0))
    return rv


class GameSheet(object):
    def __init__(self, name):
        self.workbook = xlsxwriter.Workbook(name, {'constant_memory': True})

        self.link_format = self.workbook.add_format({'color': 'blue', 'underline': 1})
        self.bold_format = self.workbook.add_format({'bold': True})
        self.under_4_format = self.workbook.add_format({'bg_color': '#E54D42', 'bold': True})
        self.under_6_format = self.workbook.add_format({'bg_color': '#E6A243', 'bold': True})
        self.under_8_format = self.workbook.add_format({'bg_color': '#29BB9C', 'bold': True})
        self.under_10_format = self.workbook.add_format({'bg_color': '#39CA74', 'bold': True})

        self.collect_sheet = self.workbook.add_worksheet(u'玩过的游戏')
        self.wish_sheet = self.workbook.add_worksheet(u'想玩的游戏')
        self.do_sheet = self.workbook.add_worksheet(u'在玩的游戏')

        collect_do_sheet_header = [u'游戏名', u'类型', u'评分', u'评分人数', u'我的评分',
                                   u'我的评语', u'标记日期', u'上市日期', u'开发商', u'平台']
        wish_sheet_header = [u'游戏名', u'类型', u'评分', u'评分人数',
                             u'标记日期', u'上市日期', u'开发商', u'平台']

        self.collect_sheet.set_column(0, 0, 25)
        self.collect_sheet.set_column(1, 1, 25)
        self.collect_sheet.set_column(5, 5, 30)
        self.collect_sheet.set_column(6, 6, 12)
        self.collect_sheet.set_column(7, 7, 12)
        self.collect_sheet.set_column(8, 8, 25)
        self.collect_sheet.set_column(9, 9, 20)

        self.do_sheet.set_column(0, 0, 25)
        self.do_sheet.set_column(1, 1, 25)
        self.do_sheet.set_column(5, 5, 30)
        self.do_sheet.set_column(6, 6, 12)
        self.do_sheet.set_column(7, 7, 12)
        self.do_sheet.set_column(8, 8, 25)
        self.do_sheet.set_column(9, 9, 20)

        self.wish_sheet.set_column(0, 0, 25)
        self.wish_sheet.set_column(1, 1, 25)
        self.wish_sheet.set_column(4, 4, 12)
        self.wish_sheet.set_column(5, 5, 12)
        self.wish_sheet.set_column(6, 6, 25)
        self.wish_sheet.set_column(7, 7, 20)

        for col, item in enumerate(collect_do_sheet_header):
            self.collect_sheet.write(0, col, item)
            self.do_sheet.write(0, col, item)

        for col, item in enumerate(wish_sheet_header):
            self.wish_sheet.write(0, col, item)

        self.collect_sheet_row = 1
        self.do_sheet_row = 1
        self.wish_sheet_row = 1

    def append_to_collect_sheet(self, game):
        if game:
            info = [[game.get('title'), game.get('url')], game.get('genre'),
                    game.get('rating'), game.get('votes'),
                    game.get('rated'), game.get('comment'),
                    game.get('date'), game.get('rdate'),
                    game.get('developer'), game.get('platform')]
            for col, item in enumerate(info):
                if col == 0:
                    self.collect_sheet.write_url(self.collect_sheet_row, col, item[1], self.link_format, item[0])
                elif col == 2 or col == 4:
                    fmt = self.bold_format
                    if item and item.strip() != '':
                        if float(item) < 4.0:
                            fmt = self.under_4_format
                        elif float(item) < 6.0:
                            fmt = self.under_6_format
                        elif float(item) < 8.0:
                            fmt = self.under_8_format
                        else:
                            fmt = self.under_10_format
                    self.collect_sheet.write(self.collect_sheet_row, col, item, fmt)
                else:
                    self.collect_sheet.write(self.collect_sheet_row, col, item)
            self.collect_sheet_row += 1

    def append_to_do_sheet(self, game):
        if game:
            info = [[game.get('title'), game.get('url')], game.get('genre'),
                    game.get('rating'), game.get('votes'),
                    game.get('rated'), game.get('comment'),
                    game.get('date'), game.get('rdate'),
                    game.get('developer'), game.get('platform')]
            for col, item in enumerate(info):
                if col == 0:
                    self.do_sheet.write_url(self.do_sheet_row, col, item[1], self.link_format, item[0])
                elif col == 2 or col == 4:
                    fmt = self.bold_format
                    if item and item.strip() != '':
                        if float(item) < 4.0:
                            fmt = self.under_4_format
                        elif float(item) < 6.0:
                            fmt = self.under_6_format
                        elif float(item) < 8.0:
                            fmt = self.under_8_format
                        else:
                            fmt = self.under_10_format
                    self.do_sheet.write(self.do_sheet_row, col, item, fmt)
                else:
                    self.do_sheet.write(self.do_sheet_row, col, item)
            self.do_sheet_row += 1

    def append_to_wish_sheet(self, game):
        if game:
            info = [[game.get('title'), game.get('url')], game.get('genre'),
                    game.get('rating'), game.get('votes'),
                    game.get('date'), game.get('rdate'),
                    game.get('developer'), game.get('platform')]
            for col, item in enumerate(info):
                if col == 0:
                    self.wish_sheet.write_url(self.wish_sheet_row, col, item[1], self.link_format, item[0])
                elif col == 2:
                    fmt = self.bold_format
                    if item and item.strip() != '':
                        if float(item) < 4.0:
                            fmt = self.under_4_format
                        elif float(item) < 6.0:
                            fmt = self.under_6_format
                        elif float(item) < 8.0:
                            fmt = self.under_8_format
                        else:
                            fmt = self.under_10_format
                    self.wish_sheet.write(self.wish_sheet_row, col, item, fmt)
                else:
                    self.wish_sheet.write(self.wish_sheet_row, col, item)
            self.wish_sheet_row += 1

    def save(self):
        self.workbook.close()

def get_game_details(data):
    subtypes = {'/collect': '玩过的游戏', '/wish': '想玩的游戏', '/do': '在玩的游戏'}
    with locks[data['category']]:
        states[data['category']][data['username']] = '正在获取' + subtypes[data['subtype']] + '信息: '\
                                            + str(data['index']) + ' / ' + str(data['total'])
    rv = data
    url = data.get('url')
    page = urlopen(url)
    soup = BeautifulSoup(page, 'html.parser')
    title = soup.find('div', id='content').find('h1')
    rating = soup.find('strong', class_='rating_num')
    votes = soup.find('span', attrs={'property': 'v:votes'})
    info = soup.find('dl', class_='game-attr')
    developer = info.find(text=re.compile(ur'开发商', re.UNICODE))
    rdate = info.find(text=re.compile(ur'发行日期', re.UNICODE))
    if rdate is None:
        rdate = info.find(text=re.compile(ur'预计上市时间', re.UNICODE))
    platform = info.find(text=re.compile(ur'平台', re.UNICODE))
    genre = info.find(text=re.compile(ur'类型', re.UNICODE))
    rv['title'] = title.string
    if rating:
        rv['rating'] = rating.string
    if votes:
        rv['votes'] = votes.string
    if developer and developer.next_element.next_element:
        rv['developer'] = developer.next_element.next_element.string.strip()
    if rdate and rdate.next_element.next_element:
        rv['rdate'] = rdate.next_element.next_element.string.strip()
    if platform and platform.next_element.next_element:
        platforms = platform.next_element.next_element.find_all('a')
        rv['platform'] = ' / '.join([p.string for p in platforms])
    if genre and genre.next_element.next_element:
        genres = genre.next_element.next_element.find_all('a')
        rv['genre'] = ' / '.join([g.string for g in genres])
    logging.info(str(data['index']) + ' / ' + str(data['total']) + ' ' + title.string)
    time.sleep(random.uniform(AVG_DELAY / 2.0, AVG_DELAY * 2.0))
    return rv

if __name__ == '__main__':
    logging.basicConfig(filename='exporter.log', format='%(asctime)s %(message)s', level=logging.INFO)
    manager = Manager()
    current_tasks = manager.Value('i', 0)
    movie_states = manager.dict()
    music_states = manager.dict()
    book_states = manager.dict()
    game_states = manager.dict()
    count_lock = manager.Lock()
    movie_lock = manager.Lock()
    music_lock = manager.Lock()
    book_lock = manager.Lock()
    game_lock = manager.Lock()
    states = {"movie": movie_states, "music": music_states, "book": book_states, "game": game_states}
    locks = {"movie": movie_lock, "music": music_lock, "book": book_lock, "game": game_lock}
    BIDS = gen_bids()
    clear_files()
    if CUSTOM_COOKIE:
        logging.info('Custom cookies detected')
    app.run('0.0.0.0', 8000)
