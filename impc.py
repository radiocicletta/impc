#!/usr/bin/env python3
# Icy Multiserver Poll Comparison
# aka IMPC
# aka IMPiCcione
import json
import sys
import pycurl
import sqlite3
import os
from time import sleep
try:
    from cStringIO import StringIO
except:
    from io import BytesIO as StringIO
import re
from datetime import datetime
import pytz

DBSCHEMA = (
    "create table server ("
    "   id integer primary key autoincrement,"
    "   name text not null,"
    "   host text not null,"
    "   port int not null default 8000,"
    "   type text not null check(type = 'icecast2:xsl' or type = 'icecast2:json' or type = 'shoutcast:icy'),"
    "   timezone text not null default 'UTC', "
    "   active integer default 1 check(active=1 or active=0)"
    ");",
    "create table entry ("
    "   time timestamp not null default current_timestamp,"
    "   local_time timestamp not null default current_timestamp,"
    "   server int not null,"
    "   listeners int not null default 0,"
    "   foreign key(server) references server(id)"
    ");",
    "create view hourly as "
    "select strftime('%s', e.local_time) as time,"
    "   s.name as name,"
    "   sum(e.listeners) as sum,"
    "   count(e.local_time) as count "
    "from entry e left join server s on (e.server=s.id) "
    "group by strftime('%Y%m%d%H00', e.local_time), e.server "
    "order by e.local_time desc;",
)

UA = "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_9_0)" \
    " AppleWebKit/537.36 (KHTML, like Gecko) Chrome/28.0.1500.71 Safari/537.36"


def status2_xsl(server):
    curl = pycurl.Curl()
    buf = StringIO()
    for protocol in ("http", "https"):
        curl.setopt(
            pycurl.URL,
            "{proto}://{host}:{port}/status2.xsl".format(
                proto=protocol,
                host=server['host'],
                port=server['port']
            )
        )
        curl.setopt(pycurl.FOLLOWLOCATION, True),
        curl.setopt(pycurl.WRITEFUNCTION, buf.write)
        curl.setopt(pycurl.USERAGENT, UA)
        try:
            curl.perform()
        except:
            pass
        else:
            listeners = re.search(
                r"Global,Clients*:\d+[ ,]Sources?:\s*\d*\s*,[^,]*,(\d+)",
                buf.getvalue().decode(),
                re.M).groups()[0]
            return int(listeners)
    return None


def status_json(server):
    curl = pycurl.Curl()
    buf = StringIO()
    for protocol in ("http", "https"):
        curl.setopt(
            pycurl.URL,
            "{proto}://{host}:{port}/status-json.xsl".format(
                proto=protocol,
                host=server['host'],
                port=server['port']
            )
        )
        curl.setopt(pycurl.FOLLOWLOCATION, True),
        curl.setopt(pycurl.WRITEFUNCTION, buf.write)
        curl.setopt(pycurl.USERAGENT, UA)
        try:
            curl.perform()
        except:
            pass
        else:
            buf.seek(0)
            js = json.loads(buf.getvalue().decode())
            if type(js['icestats']['source']) == list:
                listeners = sum([int(i['listeners']) for i in js['icestats']['source']])
            elif type(js['icestats']['source']) == dict:
                listeners = int(js['icestats']['source']['listeners'])
            return int(listeners)
    return None


def shoutcast_icy(server):
    curl = pycurl.Curl()
    buf = StringIO()
    curl.setopt(
        pycurl.URL,
        "http://{host}:{port}/".format(
            host=server['host'],
            port=server['port']
        )
    )
    curl.setopt(pycurl.FOLLOWLOCATION, True),
    curl.setopt(pycurl.WRITEFUNCTION, buf.write)
    curl.setopt(pycurl.USERAGENT, UA)
    try:
        curl.perform()
    except:
        return None
    else:
        listeners = re.search(
            r"listeners \((\d+) unique\)",
            buf.getvalue().decode(),
            re.M).groups()[0]
        return int(listeners)
    return None

methods = {
    'icecast2:xsl': status2_xsl,
    'icecast2:json': status_json,
    'shoutcast:icy': shoutcast_icy
}


def gen_output(conn):
    import pandas as pd
    import pandas.io.sql as psql
    import numpy
    from configparser import ConfigParser

    conf = ConfigParser()
    conf.read('impc.ini')

    for section in conf.sections():
        query = conf.get(section, "query")
        indexcol = conf.get(section, "indexcol")
        parsedates = conf.get(section, "parsedates")
        print(query, indexcol, parsedates)
        #return
        frame = psql.read_sql(
            query,
            conn,
            index_col=indexcol,
            parse_dates=[parsedates,]
        )

        group_median = pd.pivot_table(frame, 'listeners', index=frame.index, columns=['name'], aggfunc=numpy.median)
        group_mean = pd.pivot_table(frame, 'listeners', index=frame.index, columns=['name'], aggfunc=numpy.mean)

        for period in ('m', 'w'):

            median = group_median.resample("1" + period, how='median').interpolate(method='values')
            mean = group_mean.resample("1" + period, how='mean').interpolate(method='values')

            mean.to_json(path_or_buf="data/%s_%smean.json" % (section, period))
            median.to_json(path_or_buf="data/%s_%smedian.json" % (section, period))


def daemonize(stdin='/dev/null',
              stdout='/dev/null',
              stderr='/dev/null'):

    try:
        pid = os.fork()
        if pid > 0:
            sys.exit(0)   # Exit first parent.

    except OSError as e:
        sys.stderr.write("fork #1 failed: (%d) %s\n" % (
            e.errno, e.strerror))
        sys.exit(1)

    os.chdir("/")
    os.umask(0)
    os.setsid()

    try:
        pid = os.fork()
        if pid > 0:
            sys.exit(0)   # Exit second parent.
    except OSError as e:
        sys.stderr.write("fork #2 failed: (%d) %s\n" % (
            e.errno, e.strerror))
        sys.exit(1)

    # Redirect standard file descriptors.
    si = open(stdin, 'r')
    so = open(stdout, 'a+')
    se = open(stderr, 'a+', 0)
    os.dup2(si.fileno(), sys.stdin.fileno())
    os.dup2(so.fileno(), sys.stdout.fileno())
    os.dup2(se.fileno(), sys.stderr.fileno())



def main():
    dbpath = "./impc.sqlite"
    prepare = not os.path.exists(dbpath)
    try:
        utc = pytz.utc()
    except:
        utc = pytz.utc

    conn = sqlite3.connect(dbpath)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    if prepare:
        for sql in DBSCHEMA:
            cursor.execute(sql)

    rollbackvalues = {}

    servers = cursor.execute("select * from server").fetchall()
    if not servers:
        sys.exit()
    for server in servers:
        print(dict(server))
        rollbackvalues[server['id']] = 0

    #daemonize()

    regenerate = 0
    while True:
        now = datetime.now(utc)
        for server in cursor.execute("select * from server where active=1").fetchall():
            try:
                listeners = methods[server["type"]](dict(server))
            except:
                listeners = None
            tz = pytz.timezone(server["timezone"])
            localnow = now.astimezone(tz)

            if listeners is not None:
                cursor.execute(
                    "insert into entry (time, local_time, server, listeners) values (?, ?, ?, ?)",
                    (now, localnow, server['id'], listeners))
                rollbackvalues[server['id']] = listeners
            else:
                cursor.execute(
                    "insert into entry (time, local_time, server, listeners) values (?, ?, ?, ?)",
                    (now, localnow, server['id'], rollbackvalues[server['id']]/2))
                rollbackvalues[server['id']] = rollbackvalues[server['id']]/2
        conn.commit()

        regenerate += 600
        if regenerate == 3600:
            regenerate = 0
            pid = os.fork()
            if pid == 0:
                gen_output(conn)

        sleep(600)


if __name__ == "__main__":
    main()
