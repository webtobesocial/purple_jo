#!/usr/bin/env python3

import re
import pyasn
import socket
import argparse
import json
import uuid
import os

from datetime import datetime

from flask import jsonify
from flask_api import FlaskAPI, status
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, ServerSelectionTimeoutError
from werkzeug.exceptions import NotFound, BadRequest, BadGateway, MethodNotAllowed, RequestEntityTooLarge, InternalServerError
from werkzeug.routing import PathConverter
from logging.config import dictConfig
from flask_pymongo import PyMongo
from rejson import Client, Path
from bson import json_util


dictConfig({
    'version': 1,
    'formatters': {'default': {
        'format': '[%(asctime)s] %(levelname)s in %(module)s: %(message)s',
    }},
    'handlers': {'wsgi': {
        'class': 'logging.StreamHandler',
        'stream': 'ext://flask.logging.wsgi_errors_stream',
        'formatter': 'default'
    }},
    'root': {
        'level': 'INFO',
        'handlers': ['wsgi']
    }
})


app = FlaskAPI(__name__, static_folder=None)

app.config.from_pyfile('config.cfg')

mongo = PyMongo(app)


class EverythingConverter(PathConverter):
    regex = '.*?'


app.url_map.converters['match'] = EverythingConverter


@app.errorhandler(TypeError)
def handle_type_error(e):
    app.logger.error('type: {}, args: {}'.format(type(e).__name__, e.args))
    return jsonify(message='Something went wrong application error'), 500


@app.errorhandler(NotFound)
def handle_not_found(e):
    app.logger.error('type: {}, args: {}'.format(type(e).__name__, e.args))
    return jsonify(message='Requested resource was not found on server'), 404


@app.errorhandler(BadRequest)
def handle_bad_request(e):
    app.logger.error('type: {}, args: {}'.format(type(e).__name__, e.args))
    return jsonify(message='Bad request, the error has been reported'), 400


@app.errorhandler(BadGateway)
def handle_bad_request(e):
    app.logger.error('type: {}, args: {}'.format(type(e).__name__, e.args))
    return jsonify(message='Application programm interface is not reachable'), 502


@app.errorhandler(MethodNotAllowed)
def handle_method_not_allowed(e):
    app.logger.error('type: {}, args: {}'.format(type(e).__name__, e.args))
    return jsonify(message='The method is not allowed for the requested URL'), 405


@app.errorhandler(InternalServerError)
def handle_internal_server_error(e):
    app.logger.error('type: {}, args: {}'.format(type(e).__name__, e.args))
    return jsonify(message='Something went wrong, internal server error'), 500


@app.errorhandler(RequestEntityTooLarge)
def handle_request_entity_too_large(e):
    app.logger.error('type: {}, args: {}'.format(type(e).__name__, e.args))
    return jsonify(message='The file transmitted exceeds the capacity limit'), 413


@app.errorhandler(ServerSelectionTimeoutError)
def handle_server_selection_timeout(e):
    app.logger.error('type: {}, args: {}'.format(type(e).__name__, e.args))
    return jsonify(message='Something went wrong application error'), 500


def connect_cache():
    return Client(host='127.0.0.1', port=6379, decode_responses=True)


def fetch_one_ip(ip):
    query = {'a_record': {'$in': [ip]}}
    filter = {'_id': 0}


def cache_key(key):
    return re.sub(r'[\\\/\(\)\'\"\[\],;:#+~\. ]', '-', key)


def extra_fields(context):
    data = {'created_formatted': {'$dateToString': {'format': '%Y-%m-%d %H:%M:%S', 'date': '$created'}},
            'updated_formatted': {'$dateToString': {'format': '%Y-%m-%d %H:%M:%S', 'date': '$updated'}},
            'domain_crawled_formatted': {'$dateToString': {'format': '%Y-%m-%d %H:%M:%S', 'date': '$domain_crawled'}},
            'header_scan_failed_formatted': {'$dateToString': {'format': '%Y-%m-%d %H:%M:%S', 'date': '$header_scan_failed'}},
            'ssl.not_after_formatted': {'$dateToString': {'format': '%Y-%m-%d %H:%M:%S', 'date': '$ssl.not_after'}},
            'ssl.not_before_formatted': {'$dateToString': {'format': '%Y-%m-%d %H:%M:%S', 'date': '$ssl.not_before'}}}

    if context == 'text':
        data['score'] = {'$meta': "textScore"}

    return data


def fetch_from_cache(query, filter, sort, limit, context, cache_key):
    stored = cache.smembers(cache_key)
    cache_list = []

    if len(stored) == 0:
        store_cache(query, filter, sort, limit, context, cache_key)

        if context == 'text':
            return list(mongo.db.dns.aggregate([{'$match': query}, {'$limit': limit},
                {'$addFields': extra_fields(context)}, {'$project': filter}, {'$sort': sort}]))
        elif context == 'spatial':
            return list(mongo.db.dns.aggregate([{'$geoNear': query}, {'$limit': limit},
                {'$addFields': extra_fields(context)}, {'$project': filter}, {'$sort': sort}]))
        else:
            return list(mongo.db.dns.aggregate([{'$match': query}, {'$limit': limit},
                {'$addFields': extra_fields(context)}, {'$project': filter}, {'$sort': sort}]))

    for store in stored:
        cache_list.append(cache.jsonget(store, Path.rootPath()))

    return cache_list


def store_cache(query, filter, sort, limit, context, cache_key, reset=False):
    if reset:
        cache.delete(cache_key)

    if context == 'text':
        docs = mongo.db.dns.aggregate([{'$match': query}, {'$limit': limit},
            {'$addFields': extra_fields(context)}, {'$project': filter}, {'$sort': sort}])
    elif context == 'spatial':
        docs = mongo.db.dns.aggregate([{'$geoNear': query}, {'$limit': limit},
            {'$addFields': extra_fields(context)}, {'$project': filter}, {'$sort': sort}])
    else:
        docs = mongo.db.dns.aggregate([{'$match': query}, {'$limit': limit},
            {'$addFields': extra_fields(context)}, {'$project': filter}, {'$sort': sort}])

    for doc in docs:
        uid = hash(uuid.uuid4())
        expire = 3600 * 24
        cache.jsonset(uid, Path.rootPath(), json.loads(
            json.dumps(doc, default=json_util.default)))
        cache.sadd(cache_key, uid)
        cache.expire(cache_key, expire)
        cache.expire(uid, expire)


def create_index(field_name_1, field_name_2):
    mongo.db.dns.create_index(
        [(field_name_1, DESCENDING), (field_name_2, DESCENDING)], background=True)


def fetch_match_condition(condition, query):
    if query is not None:
        if condition == 'registry':
            sub_query = query.lower()

            query = {'whois.asn_registry': sub_query}
            filter = {'_id': 0}
            sort = {'updated': -1}
            context = 'normal'
            limit = 30

            return fetch_from_cache(query, filter, sort, limit, context, 'registry-{}'.format(cache_key(sub_query)))
        elif condition == 'port':
            sub_query = query

            query = {'ports.port': int(query)}
            filter = {'_id': 0}
            sort = {'updated': -1}
            context = 'normal'
            limit = 30

            return fetch_from_cache(query, filter, sort, limit, context, 'port-{}'.format(cache_key(sub_query)))
        elif condition == 'status':
            sub_query = query

            query = {'header.status': sub_query}
            filter = {'_id': 0}
            sort = {'updated': -1}
            context = 'normal'
            limit = 30

            return fetch_from_cache(query, filter, sort, limit, context, 'status-{}'.format(cache_key(sub_query)))
        elif condition == 'ssl':
            sub_query = query.lower()

            query = {'$or': [{'ssl.subject.common_name': sub_query},
                             {'ssl.subject_alt_names': {'$in': [sub_query]}}]}
            filter = {'_id': 0}
            sort = {'updated': -1}
            context = 'normal'
            limit = 30

            return fetch_from_cache(query, filter, sort, limit, context, 'ssl-{}'.format(cache_key(sub_query)))
        elif condition == 'before':
            sub_query = query.lower()

            query = {'ssl.not_before': {
                '$gte': datetime.strptime(query, '%Y-%m-%d %H:%M:%S')}}
            filter = {'_id': 0}
            sort = {'updated': -1}
            context = 'normal'
            limit = 30

            return fetch_from_cache(query, filter, sort, limit, context, 'before-{}'.format(cache_key(sub_query)))
        elif condition == 'after':
            sub_query = query.lower()

            query = {'ssl.not_after': {
                '$lte': datetime.strptime(query, '%Y-%m-%d %H:%M:%S')}}
            filter = {'_id': 0}
            sort = {'updated': -1}
            context = 'normal'
            limit = 30

            return fetch_from_cache(query, filter, sort, limit, context, 'after-{}'.format(cache_key(sub_query)))
        elif condition == 'ca':
            sub_query = query.lower()

            query = {'ssl.ca_issuers': query}
            filter = {'_id': 0}
            sort = {'updated': -1}
            context = 'normal'
            limit = 30

            return fetch_from_cache(query, filter, sort, limit, context, 'ca-{}'.format(cache_key(sub_query)))
        elif condition == 'issuer':
            sub_query = query.lower()

            query = {'$or': [{'ssl.issuer.organization_name': query},
                    {'ssl.issuer.common_name': query}]}
            filter = {'_id': 0}
            sort = {'updated': -1}
            context = 'normal'
            limit = 30

            return fetch_from_cache(query, filter, sort, limit, context, 'issuer-{}'.format(cache_key(sub_query)))
        elif condition == 'unit':
            sub_query = query.lower()

            query = {'ssl.issuer.organizational_unit_name': query}
            filter = {'_id': 0}
            sort = {'updated': -1}
            context = 'normal'
            limit = 30

            return fetch_from_cache(query, filter, sort, limit, context, 'unit-{}'.format(cache_key(sub_query)))
        elif condition == 'ocsp':
            sub_query = query.lower()

            query = {'ssl.ocsp': query}
            filter = {'_id': 0}
            sort = {'updated': -1}
            context = 'normal'
            limit = 30

            return fetch_from_cache(query, filter, sort, limit, context, 'ocsp-{}'.format(cache_key(sub_query)))
        elif condition == 'crl':
            sub_query = query.lower()

            query = {'ssl.crl_distribution_points': query}
            filter = {'_id': 0}
            sort = {'updated': -1}
            context = 'normal'
            limit = 30

            return fetch_from_cache(query, filter, sort, limit, context, 'crl-{}'.format(cache_key(sub_query)))
        elif condition == 'service':
            sub_query = query.lower()

            query = {'header.x-powered-by': query}
            filter = {'_id': 0}
            sort = {'updated': -1}
            context = 'normal'
            limit = 30

            return fetch_from_cache(query, filter, sort, limit, context, 'service-{}'.format(cache_key(sub_query)))
        elif condition == 'country':
            sub_query = query.upper()

            query = {'$and': [{'geo.country_code': query},
                              {'whois.asn_country_code': query}]}
            filter = {'_id': 0}
            sort = {'updated': -1}
            context = 'normal'
            limit = 30

            return fetch_from_cache(query, filter, sort, limit, context, 'country-{}'.format(cache_key(sub_query)))
        elif condition == 'state':
            sub_query = query.lower()

            query = {'geo.state': query}
            filter = {'_id': 0}
            sort = {'updated': -1}
            context = 'normal'
            limit = 30

            return fetch_from_cache(query, filter, sort, limit, context, 'state-{}'.format(cache_key(sub_query)))
        elif condition == 'city':
            sub_query = query.lower()

            query = {'geo.city': query}
            filter = {'_id': 0}
            sort = {'updated': -1}
            context = 'normal'
            limit = 30

            return fetch_from_cache(query, filter, sort, limit, context, 'city-{}'.format(cache_key(sub_query)))
        elif condition == 'loc':
            sub_query = query.lower()
            splited = query.split(',')

            try:
                query = {'distanceField': 'geo.distance',
                    'near': {'type': 'Point',
                    'coordinates': [float(splited[0]), float(splited[1])]},
                    'maxDistance': 50000, 'spherical': True}
                filter = {'_id': 0}
                sort = {'updated': -1}
                context = 'spatial'
                limit = 30

                return fetch_from_cache(query, filter, sort, limit, context, 'loc-{}'.format(cache_key(sub_query)))
            except ValueError:
                return []
        elif condition == 'banner':
            sub_query = query.lower()

            query = {'banner': query}
            filter = {'_id': 0}
            sort = {'updated': -1}
            context = 'normal'
            limit = 30

            return fetch_from_cache(query, filter, sort, limit, context, 'banner-{}'.format(cache_key(sub_query)))
        elif condition == 'asn':
            sub_query = re.sub(r'[a-zA-Z:]', '', query.lower())

            query = {'whois.asn': sub_query}
            filter = {'_id': 0}
            sort = {'updated': -1}
            context = 'normal'
            limit = 30

            return fetch_from_cache(query, filter, sort, limit, context, 'asn-{}'.format(cache_key(sub_query)))
        elif condition == 'org':
            sub_query = re.sub(r'[\(\)]', '', query.lower())

            query = {'whois.asn_description': query}
            filter = {'_id': 0}
            sort = {'updated': -1}
            context = 'normal'
            limit = 30

            return fetch_from_cache(query, filter, sort, limit, context, 'org-{}'.format(cache_key(sub_query)))
        elif condition == 'cidr':
            sub_query = query.lower()

            query = {'whois.asn_cidr': query}
            filter = {'_id': 0}
            sort = {'updated': -1}
            context = 'normal'
            limit = 30

            return fetch_from_cache(query, filter, sort, limit, context, 'cidr-{}'.format(cache_key(sub_query)))
        elif condition == 'cname':
            sub_query = query.lower()

            query = {'cname_record.target': {'$in': [sub_query]}}
            filter = {'_id': 0}
            sort = {'updated': -1}
            context = 'normal'
            limit = 30

            return fetch_from_cache(query, filter, sort, limit, context, 'cname-{}'.format(cache_key(sub_query)))
        elif condition == 'mx':
            sub_query = query.lower()

            query = {'mx_record.exchange': {'$in': [sub_query]}}
            filter = {'_id': 0}
            sort = {'updated': -1}
            context = 'normal'
            limit = 30

            return fetch_from_cache(query, filter, sort, limit, context, 'mx-{}'.format(cache_key(sub_query)))
        elif condition == 'ns':
            sub_query = query.lower()

            query = {'ns_record': {'$in': [sub_query]}}
            filter = {'_id': 0}
            sort = {'updated': -1}
            context = 'normal'
            limit = 30

            return fetch_from_cache(query, filter, sort, limit, context, 'ns-{}'.format(cache_key(sub_query)))
        elif condition == 'server':
            sub_query = query.lower()

            query = {'header.server': query}
            filter = {'_id': 0}
            sort = {'updated': -1}
            context = 'normal'
            limit = 30

            return fetch_from_cache(query, filter, sort, limit, context, 'server-{}'.format(cache_key(sub_query)))
        elif condition == 'site':
            sub_query = query.lower()

            query = {'domain': sub_query}
            filter = {'_id': 0}
            sort = {'updated': -1}
            context = 'normal'
            limit = 30

            return fetch_from_cache(query, filter, sort, limit, context, 'site-{}'.format(cache_key(sub_query)))
        elif condition == 'ipv4':
            sub_query = query.lower()

            query = {'a_record': {'$in': [query]}}
            filter = {'_id': 0}
            sort = {'updated': -1}
            context = 'normal'
            limit = 30

            return fetch_from_cache(query, filter, sort, limit, context, 'ipv4-{}'.format(cache_key(sub_query)))
        elif condition == 'ipv6':
            sub_query = query.lower()

            query = {'aaaa_record': {'$in': [query]}}
            filter = {'_id': 0}
            sort = {'updated': -1}
            context = 'normal'
            limit = 30

            return fetch_from_cache(query, filter, sort, limit, context, 'ipv6-{}'.format(cache_key(sub_query)))


def fetch_all_prefix(prefix):
    return mongo.db.lookup.find({'cidr': {'$in': [prefix]}}, {'_id': 0})


def fetch_all_asn(asn):
    return mongo.db.lookup.find({'whois.asn': asn}, {'_id': 0}).limit(5)


def fetch_query_domain(q):
    sub_query = q.lower()

    query = {'$text': {'$search': q}}
    filter = {'_id': 0}
    sort = {'score': {'$meta': 'textScore'}}
    context = 'text'
    limit = 30

    return fetch_from_cache(query, filter, sort, limit, context, 'all-{}'.format(cache_key(sub_query)))


def fetch_latest_dns():
    query = {'updated': {'$exists': True}, 'scan_failed': {'$exists': False}}
    filter = {'_id': 0}
    sort = {'updated': -1}
    context = 'normal'
    limit = 200

    return fetch_from_cache(query, filter, sort, limit, context, 'latest_dns')


def fetch_latest_cidr():
    query = {'whois.asn_cidr': {'$exists': True}}
    filter = {'_id': 0, 'whois.asn_country_code': 1, 'whois.asn_cidr': 1}
    sort = {'updated': -1}
    context = 'normal'
    limit = 200

    return fetch_from_cache(query, filter, sort, limit, context, 'latest_cidr')


def fetch_latest_ipv4():
    query = {'a_record': {'$exists': True}}
    filter = {'_id': 0, 'a_record': 1, 'country_code': 1}
    sort = {'updated': -1}
    context = 'normal'
    limit = 200

    return fetch_from_cache(query, filter, sort, limit, context, 'ilatest_pv4')


def fetch_latest_asn():
    query = {'whois.asn': {'$exists': True}}
    filter = {'_id': 0, 'whois.asn': 1, 'whois.asn_country_code': 1}
    sort = {'updated': -1}
    context = 'normal'
    limit = 200

    return fetch_from_cache(query, filter, sort, limit, context, 'latest_asn')


def asn_lookup(ipv4):
    asndb = pyasn.pyasn('rib.20191127.2000.dat',
                        as_names_file='asn_names.json')
    asn, prefix = asndb.lookup(ipv4)
    name = asndb.get_as_name(asn)

    return {'prefix': prefix, 'name': name, 'asn': asn}


@app.route('/query/<string:domain>', methods=['GET'])
def fetch_data_domain(domain):
    data = list(fetch_query_domain(domain))

    if data:
        return jsonify(data)
    else:
        return jsonify({'status': 404, 'message': 'no documents found'}), status.HTTP_404_NOT_FOUND


@app.route('/subnet/<string:sub>/<string:prefix>', methods=['GET'])
def fetch_data_prefix(sub, prefix):
    data = list(fetch_all_prefix('{}/{}'.format(sub, prefix)))

    if data:
        return jsonify(data)
    else:
        return jsonify({'status': 404, 'message': 'no documents found'}), status.HTTP_404_NOT_FOUND


@app.route('/match/<path:query>', methods=['GET'])
def fetch_data_condition(query):
    ql = query.split(':')
    f = ql[0].lower()

    if f == 'ipv6' or f == 'ca' or f == 'crl' or f == 'ocsp' or f == 'before' or f == 'after':
        q = ':'.join(ql[1:])
    else:
        q = ql[1]

    data = list(fetch_match_condition(f, q))

    if data:
        return jsonify(data)
    else:
        return jsonify({'status': 404, 'message': 'no documents found'}), status.HTTP_404_NOT_FOUND


@app.route('/dns/', methods=['GET'])
@app.route('/dns', methods=['GET'])
def fetch_latest_dns_data():
    data = fetch_latest_dns()

    if data:
        return jsonify(data)
    else:
        return jsonify({'status': 404, 'message': 'no documents found'}), status.HTTP_404_NOT_FOUND


@app.route('/asn', methods=['GET'])
def fetch_latest_asn_data():
    data = fetch_latest_asn()

    if data:
        return jsonify(data)
    else:
        return jsonify({'status': 404, 'message': 'no documents found'}), status.HTTP_404_NOT_FOUND


@app.route('/cidr', methods=['GET'])
def fetch_latest_cidr_data():
    data = fetch_latest_cidr()

    if data:
        return jsonify(data)
    else:
        return jsonify({'status': 404, 'message': 'no documents found'}), status.HTTP_404_NOT_FOUND


@app.route('/ipv4', methods=['GET'])
def fetch_latest_ipv4_data():
    data = fetch_latest_ipv4()
    v = []

    for d in data:
        if 'country_code' in d:
            c = d['country_code']
        else:
            c = None

        for r in d['a_record']:
            v.append({'a_record': r, 'country_code': c})

    if v:
        return jsonify(v)
    else:
        return jsonify({'status': 404, 'message': 'no documents found'}), status.HTTP_404_NOT_FOUND


@app.route('/', methods=['GET'])
def fetch_nothing():
    return jsonify({'status': 404, 'message': 'no documents found'}), status.HTTP_404_NOT_FOUND


@app.route('/ip/<string:ipv4>', methods=['GET'])
def fetch_data_ip(ipv4):
    data = list(fetch_one_ip(ipv4))

    if len(data) == 0:
        res = asn_lookup(ipv4)

        try:
            host = socket.gethostbyaddr(ipv4)[0]
        except Exception:
            host = None

        prop = {'ip': ipv4, 'host': host, 'updated': datetime.utcnow(),
                'asn': res['asn'], 'name': res['name'], 'cidr': [res['prefix']]}

        try:
            mongo.db.lookup.insert_one(prop)
        except DuplicateKeyError:
            pass

        if '_id' in prop:
            del prop['_id']

        data = [prop]

    if data:
        return jsonify(data)
    else:
        return jsonify({'status': 404, 'message': 'no documents found'}), status.HTTP_404_NOT_FOUND


def argparser():
    parser = argparse.ArgumentParser()
    parser.add_argument('--debug', help='debug flag', type=bool, default=False)
    parser.add_argument('--port', help='port number', type=int, required=True)
    args = parser.parse_args()

    return args


cache = connect_cache()

# create index for all match methods
create_index('header.x-powered-by', 'updated')
create_index('banner', 'updated')
create_index('ports.port', 'updated')

# create index for whois section
create_index('whois.asn', 'updated')
create_index('whois.asn_description', 'updated')
create_index('whois.asn_country_code', 'updated')
create_index('whois.asn_registry', 'updated')
create_index('whois.asn_cidr', 'updated')

# create index for records section
create_index('cname_record.target', 'updated')
create_index('mx_record.exchange', 'updated')
create_index('header.server', 'updated')
create_index('header.status', 'updated')
create_index('ns_record', 'updated')
create_index('aaaa_record', 'updated')
create_index('a_record', 'updated')
create_index('domain', 'updated')

# create index for geo section
create_index('geo.loc.coordinates', 'updated')
create_index('geo.country_code', 'updated')
create_index('geo.country', 'updated')
create_index('geo.state', 'updated')
create_index('geo.city', 'updated')
create_index('ssl.ocsp', 'updated')

# create index for ssl section
create_index('ssl.not_after', 'updated')
create_index('ssl.not_before', 'updated')
create_index('ssl.ca_issuers', 'updated')
create_index('ssl.issuer.common_name', 'updated')
create_index('ssl.issuer.organization_name', 'updated')
create_index('ssl.issuer.organizational_unit_name', 'updated')
create_index('ssl.subject_alt_names', 'updated')
create_index('ssl.subject.common_name', 'updated')
create_index('ssl.crl_distribution_points', 'updated')


if __name__ == '__main__':
    args = argparser()
    app.run(port=args.port, debug=args.debug)
