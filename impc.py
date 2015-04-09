#!/usr/bin/env
# Icy Multiserver Poll Comparison
# aka IMPC
# aka IMPiCcione
import json
import sys
import pycurl
import sqlite3
import os
from time import sleep
from cStringIO import StringIO
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
                "Global,Clients*:\d+ Source:\s*\d*\s*,[^,]*,(\d+)",
                buf.getvalue(),
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
            js = json.load(buf)
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
            "listeners \((\d+) unique\)",
            buf.getvalue(),
            re.M).groups()[0]
        return int(listeners)
    return None

methods = {
    'icecast2:xsl': status2_xsl,
    'icecast2:json': status_json,
    'shoutcast:icy': shoutcast_icy
}

def daemonize(self,
              stdin='/dev/null',
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


if __name__ == "__main__":
    dbpath = "./impc.sqlite"
    prepare = not os.path.exists(dbpath)
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
        print server
        rollbackvalues[server['id']] = 0

    #daemonize()

    while True:
        now = datetime.now(utc)
        for server in cursor.execute("select * from server where active=1").fetchall():
            try:
                listeners = methods[server["type"]](server)
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
        sleep(600)
