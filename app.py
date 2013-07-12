from __future__ import division

import csv
import json
import os
import urllib

from collections import defaultdict, OrderedDict
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import groupby

import boto
from boto.s3.key import Key

import grequests
import requests

from flask import Flask, render_template, request
from gevent.pywsgi import WSGIServer
from werkzeug.contrib.cache import MemcachedCache

import local

log_cache = os.path.join(os.path.dirname(__file__), 'cache')
cache = MemcachedCache([os.getenv('MEMCACHE_URL', 'localhost:11211')])

app = Flask(__name__)

servers = {
    'dev': 'https://marketplace-dev.allizom.org',
    'stage': 'https://marketplace.allizom.org',
    'prod': 'https://marketplace.firefox.com'
}

api = {
    'tiers': '/api/v1/webpay/prices'
}

regions = {
    1: 'Worldwide', 2: 'US', 4: 'UK', 7: 'Brazil', 8: 'Spain', 9: 'Colombia',
    10: 'Venezuela', 11: 'Poland', 12: 'Mexico', 13: 'Hungary', 14: 'Germany'
}

methods = {
    0: 'operator',
    1: 'card',
    2: 'both'
}

regions_sorted = sorted(regions.keys())

builds = {
    'jenkins': ['solitude', 'marketplace', 'marketplace-api',
                'marketplace-webpay', 'amo-master', 'solitude'],
    'travis': ['andymckay/receipts', 'mozilla/fireplace',
               'andymckay/django-paranoia', 'andymckay/curling',
               'andymckay/django-statsd']
}

statuses = {
    '0': ['pending', 'null'],
    '1': ['completed', 'success'],
    '2': ['checked', 'info'],
    '3': ['received', 'info'],
    '4': ['failed', 'important'],
    '5': ['cancelled', 'warning'],
}


def notify(msg, *args):
    esc = urllib.urlencode(dict(['args', a] for a in args), doseq=True)
    url = 'https://notify.paas.allizom.org/notify/{0}/?{1}'.format(msg, esc)
    requests.post(url, headers={'Authorization':
                                'Basic {0}'.format(local.NOTIFY_AUTH)})

@app.route('/')
def base(name=None):
    return render_template('index.html', name=name)


def get_jenkins(keys, results):
    reqs = []
    for key in keys:
        url = ('https://ci.mozilla.org/job/{0}/lastCompletedBuild/api/json'
               .format(key))
        reqs.append(grequests.get(url, headers={'Accept': 'application/json'}))

    resps = grequests.map(reqs)
    for key, resp in zip(keys, resps):
        results['results'][key] = resp.json()['result'] == 'SUCCESS'

    return results

def get_travis(keys, results):
    reqs = []
    for key in keys:
        url = ('https://api.travis-ci.org/repositories/{0}.json'
               .format(key))
        reqs.append(grequests.get(url, headers={'Accept': 'application/json'}))

    resps = grequests.map(reqs)
    for key, resp in zip(keys, resps):
        results['results'][key] = resp.json()['last_build_result'] == 0

    return results


def get_build():
    result = cache.get('build')

    if not result:
        result = {'when': datetime.now(), 'results': {}}
        get_jenkins(builds['jenkins'], result)
        get_travis(builds['travis'], result)
        cache.set('build', result, timeout=60 * 5)

    result['results'] = OrderedDict(sorted(result['results'].items()))
    passing = all(result['results'].values())
    last = bool(cache.get('last-build'))

    cache.set('last-build', True)
    if last is None:
        cache.set('last-build', bool(passing))

    elif last != passing:
        #notify('builds', 'passing' if passing else 'failing')
        cache.set('last-build', bool(passing))

    return result, passing


@app.route('/build/')
def build():
    result, passing = get_build()

    if 'application/json' in request.headers['Accept']:
        result['when'] = result['when'].isoformat()
        return json.dumps({'all': passing, 'result': result})

    return render_template('build.html', result=result, request=request,
                           all=passing)


def fill_tiers(result):
    for tier in result['objects']:
        prices = {}
        for price in tier['prices']:
            prices[price['region']] = price
        tier['prices'] = prices

    return result


@app.route('/tiers/')
@app.route('/tiers/<server>/')
def tiers(server=None):
    if server:
        res = requests.get('{0}{1}'.format(
            servers[server], api['tiers']))
        result = fill_tiers(res.json())
        return render_template('tiers.html', result=result['objects'],
                               regions=regions, sorted=regions_sorted,
                               methods=methods, server=server)

    return render_template('tiers.html')


def s3_get(filename):
    conn = boto.connect_s3(local.S3_AUTH['key'], local.S3_AUTH['secret'])
    bucket = conn.get_bucket(local.S3_BUCKET)
    k = Key(bucket)
    k.key = filename
    k.get_contents_to_filename(os.path.join(log_cache, filename))


def list_to_dict_multiple(listy):
    return reduce(lambda x, (k,v): x[k].append(v) or x, listy, defaultdict(list))


@app.route('/transactions/')
@app.route('/transactions/<server>/<date>/')
def transactions(server=None, date=''):
    sfmt = '%Y-%m-%d'
    lfmt = sfmt + 'T%H:%M:%S'
    today = datetime.today()
    dates = (('Yesterday', (today - timedelta(days=1)).strftime(sfmt)),
             ('-2 days', (today - timedelta(days=2)).strftime(sfmt)))

    if server and date:
        date = datetime.strptime(date, sfmt)
        filename = date.strftime(sfmt) + '.log'
        if filename not in os.listdir(log_cache):
            s3_get(filename)

        src = os.path.join(log_cache, filename)
        with open(src) as csvfile:
            rows = []
            stats = defaultdict(list)
            for row in csv.DictReader(csvfile):
                row['created'] = datetime.strptime(row['created'], lfmt)
                row['modified'] = datetime.strptime(row['modified'], lfmt)
                row['diff'] = row['modified'] - row['created']
                if row['diff']:
                    stats['diff'].append(row['diff'].total_seconds())
                stats['status'].append(row['status'])

                if row['currency'] and row['amount']:
                    stats['currencies'].append((row['currency'],
                                                Decimal(row['amount'])))

                rows.append(row)

            if len(stats['diff']):
                stats['mean'] = '%.2f' % (sum(stats['diff'])/len(stats['diff']))

            for status, group in groupby(sorted(stats['status'])):
                group = len(list(group))
                perc = (group / len(stats['status'])) * 100
                stats['statuses'].append((str(status), '%.2f' % perc))

            stats['currencies'] = list_to_dict_multiple(stats['currencies'])
            for currency, items in stats['currencies'].items():
                stats['currencies'][currency] = {'items': items}
                stats['currencies'][currency]['count'] = len(items)
                mean = (sum(items) / len(items))
                stats['currencies'][currency]['mean'] = '%.2f' % mean

            return render_template('transactions.html', rows=rows,
                                   server=server, dates=dates, stats=stats,
                                   statuses=statuses, filename=filename)
    return render_template('transactions.html', dates=dates)


@app.errorhandler(500)
def page_not_found(err):
    return render_template('500.html', err=err), 500


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.debug = True
    http = WSGIServer(('0.0.0.0', port), app)
    http.serve_forever()
