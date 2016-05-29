#!/usr/bin/env python3
import sys
from contextlib import suppress
from urllib.parse import urljoin, urlsplit

import requests
from lxml import etree
from lxml.cssselect import CSSSelector

# key: url, value: referers
referers = dict()
# key: url, value: http response code
visited = dict()
queued = set()
ignored = set()


def link_extractor(html, url):
    img_selector = CSSSelector('img')
    a_selector = CSSSelector('a')

    def urls_generator():
        for img in img_selector(html):
            with suppress(KeyError):
                yield urljoin(url, img.attrib['src'])
            with suppress(KeyError):
                yield urljoin(url, img.attrib['data-src'])

        for a in a_selector(html):
            with suppress(KeyError):
                yield urljoin(url, a.attrib['href'])

    return set(urls_generator())

if __name__ == '__main__':
    # urls are SCHEME://NETLOC/PATH
    recursive_netlocs = set()
    for start_url in sys.argv[1:]:
        queued.add(start_url)
        referers[start_url] = set(['commandline'])
        recursive_netlocs.add(urlsplit(start_url).netloc)

    while queued:
        url = queued.pop()
        r = requests.get(url)
        visited[url] = r.status_code
        if r.status_code == 200 and \
                r.headers['content-type'].startswith('text/html') and \
                urlsplit(url).netloc in recursive_netlocs:
            for found_url in link_extractor(etree.HTML(r.content), url):
                if urlsplit(found_url).scheme not in ['http', 'https']:
                    continue
                try:
                    referers[found_url].add(url)
                    assert (found_url in visited) or (found_url in queued)
                    # no KeyError? => we've already seen this one
                except KeyError:
                    referers[found_url] = set([url])
                    queued.add(found_url)

    for url, code in sorted(visited.items(), key=lambda e: e[0]):
        if code >= 400:
            print("{}: {}\nFound on:".format(code, url))
            for ref in referers[url]:
                print("    {}".format(ref))
