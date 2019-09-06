#!/usr/bin/env python3
import os
import sys
import http.server
from threading import Event, Thread, Condition, \
    BoundedSemaphore
from contextlib import suppress
from urllib.parse import urljoin, urlsplit

import requests
from lxml import etree
from lxml.cssselect import CSSSelector

DEBUG = False


class LinkChecker(object):
    def __init__(self, start_urls):
        # urls are SCHEME://NETLOC/PATH
        self.recursive_netlocs = set(urlsplit(u).netloc for u in start_urls)
        # key: url, value: referers
        self.referers = dict((u, set(['commandline'])) for u in start_urls)
        # key: url, value: http response code
        self.visited = dict()
        self.queued = set(start_urls)
        self.ignored = set()
        self.cv = Condition()
        self.waiting = 0
        self.num_workers = 0
        self.done = Event()

    def link_extractor(self, html, url):
        # obvious
        img_selector = CSSSelector('img')
        a_selector = CSSSelector('a')
        script_selector = CSSSelector('script')

        # stylesheets, rel="preload"-stuff and much more
        link_selector = CSSSelector('link')
        # HTML5 <picture>-Element
        source_selector = CSSSelector('source')

        def urls_generator():
            for img in img_selector(html):
                with suppress(KeyError):
                    yield urljoin(url, img.attrib['src'])
                with suppress(KeyError):
                    yield urljoin(url, img.attrib['data-src'])
                with suppress(KeyError):
                    # format: url [width], ...
                    for src in img.attrib['srcset'].split(','):
                        src = src.strip().split()[0]
                        yield urljoin(url, src)

            for a in a_selector(html):
                with suppress(KeyError):
                    yield urljoin(url, a.attrib['href'])

            for s in script_selector(html):
                with suppress(KeyError):
                    yield urljoin(url, s.attrib['src'])

            for l in link_selector(html):
                with suppress(KeyError):
                    yield urljoin(url, l.attrib['href'])

            for s in source_selector(html):
                with suppress(KeyError):
                    yield urljoin(url, s.attrib['src'])
                with suppress(KeyError):
                    # format: url [width], ...
                    for src in s.attrib['srcset'].split(','):
                        src = src.strip().split()[0]
                        yield urljoin(url, src)

        return set(u.split('#')[0] for u in urls_generator())

    def process_queued_urls(self, idx):
        id = "Thread {}:".format(idx)
        while True:
            with self.cv:
                try:
                    url = self.queued.pop()
                except KeyError:
                    if DEBUG:
                        print(id, "waiting...")
                    self.waiting += 1
                    if self.num_workers == self.waiting:
                        # every thread is waiting => done
                        self.done.set()
                        self.cv.notify_all()
                        if DEBUG:
                            print(id, "done")
                        return
                    else:
                        self.cv.wait()
                        self.waiting -= 1
                        continue
            if DEBUG:
                print(id, url)
            ua = 'link_checker (https://github.com/Delphinator/link_checker)'
            r = requests.get(url, headers={'User-Agent': ua})

            with self.cv:
                self.visited[url] = r.status_code
                if r.status_code == 200 and \
                        r.headers['content-type'].startswith('text/html') and \
                        urlsplit(url).netloc in self.recursive_netlocs:
                    for found_url in self.link_extractor(etree.HTML(r.content), url):
                        if urlsplit(found_url).scheme not in ['http', 'https']:
                            continue
                        try:
                            self.referers[found_url].add(url)
                            # no KeyError? => we've already seen this one
                        except KeyError:
                            self.referers[found_url] = set([url])
                            self.queued.add(found_url)
                            self.cv.notify()

    def run(self, num_workers):
        threadpool = [Thread(target=self.process_queued_urls, args=(i,), daemon=False)
                      for i in range(num_workers)]
        self.waiting = 0
        self.num_workers = num_workers
        self.done.clear()
        [t.start() for t in threadpool]
        [t.join() for t in threadpool]


if __name__ == '__main__':
    urls = sys.argv[1:]
    local_server_thread = None
    if len(urls) == 0:
        print('checking links in files found in current directory')
        for r, _, files in os.walk('.'):
            for f in files:
                f = os.path.join(r, f)
                if f.startswith('./'):
                    f = f[2:]
                if f.endswith('index.html'):
                    f = f[:-len('index.html')]
                urls.append('http://127.0.0.1:8000/' + f)

        local_server_thread = Thread(
            target=lambda: http.server.test(
                HandlerClass=http.server.SimpleHTTPRequestHandler,
                bind="127.0.0.1"
            ),
            daemon=True
        )
        local_server_thread.start()

    link_checker = LinkChecker(urls)

    link_checker.run(10)

    print("checked {} urls, {} returned errors.".format(
        len(link_checker.visited),
        [code >= 400 for code in link_checker.visited.values()].count(True)
    ))

    for url, code in sorted(link_checker.visited.items(), key=lambda e: e[0]):
        if code >= 400:
            print("{}: {}\nFound on:".format(code, url))
            for ref in link_checker.referers[url]:
                print("    {}".format(ref))

    if any(code >= 400 for code in link_checker.visited.values()):
        sys.exit(1)
