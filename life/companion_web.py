"""Read real pages; a search snippet is never evidence that a page was read."""
import asyncio
import json
import re
import time
import uuid
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urlencode, urljoin, urlsplit
from xml.etree import ElementTree

CHUNK = 4000
# 这台机器上 Bing 的 RSS 与网页搜索都会返回与查询无关的内容（导航站、游戏论坛），
# 所以只走国内引擎，并在采用前核对结果是否真的回应了查询。
# 搜狗对这个出口一律返回验证码页，不再尝试。
# 百度必须带完整浏览器请求头，否则会被重定向到 wappass 验证码页。
ENGINES = (
    ('baidu', 'https://www.baidu.com/s?wd=', 'https://www.baidu.com', 'https://www.baidu.com/'),
    ('so360', 'https://www.so.com/s?q=', 'https://www.so.com', ''),
)
SEARCH_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/124.0 Safari/537.36'),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Accept-Encoding': 'gzip, deflate',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
}
SEARCH_UA = SEARCH_HEADERS['User-Agent']
NAV_TITLES = ('其他人还搜了', '相关搜索', '大家还在搜', '查看更多', '上一页', '下一页',
              '百度图片', '精选笔记', '文库精选', '相关消息', '的最新相关信息', '相关视频')
# 聚合页和验证码页不是可以实际打开的网页。
BLOCKED_URLS = ('ai.so.com', 'image.so.com', 'news.so.com/ns?', 'wappass.baidu.com')


def clean_text(markup):
    return ' '.join(unescape(re.sub(r'<[^>]+>', ' ', markup)).split())


def parse_results(raw, host):
    """国内引擎的结果块都以 <h3> 包标题，按块切分即可拿到标题、链接和后面的摘要。"""
    items = []
    for part in re.split(r'<h3\b', raw, flags=re.I)[1:]:
        block = '<h3' + part
        title = re.search(r'<h3[^>]*>(.*?)</h3>', block, re.S)
        href = re.search(r'<a[^>]+href="([^"]+)"', block)
        if not title or not href:
            continue
        text = clean_text(title.group(1))
        link = href.group(1)
        if not text or not link.startswith(('http', '/')):
            continue
        if any(nav in text for nav in NAV_TITLES):
            continue
        url = urljoin(host, link)
        # 引擎自己的导航/“再搜一次”链接不是搜索结果；跳转链接保留，open 会跟随它。
        if url.startswith(host) and '/link?' not in url:
            continue
        if any(blocked in url for blocked in BLOCKED_URLS):
            continue
        snippet = block[title.end():title.end() + 6000]
        snippet = re.sub(r'<(script|style)\b.*?</\1>', ' ', snippet, flags=re.S | re.I)
        snippet = re.sub(r'<[^>]*$', ' ', snippet)   # 窗口可能截在标签中间
        snippet = clean_text(snippet)[:200]
        items.append(dict(title=text[:120], url=url, snippet=snippet))
        if len(items) >= 12:
            break
    return items


def results_relevant(query, items):
    """搜索结果必须真的回应查询：无关内容不能当作搜到了东西。"""
    terms = [term for term in re.split(r'\s+', query) if len(term) >= 2]
    if not terms or not items:
        return False
    matched = sum(1 for item in items if any(term in item['title'] for term in terms))
    return matched >= max(1, len(items) // 5)


class _Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ''
        self._in_title = False
        self._skip = 0
        self.text = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == 'title':
            self._in_title = True
        elif tag in {'script', 'style', 'noscript', 'nav', 'footer', 'aside', 'header'}:
            self._skip += 1
        elif tag == 'a':
            href = dict(attrs).get('href')
            if href and len(self.links) < 40:
                self.links.append(href)

    def handle_endtag(self, tag):
        if tag == 'title':
            self._in_title = False
        elif tag in {'script', 'style', 'noscript', 'nav', 'footer', 'aside', 'header'} and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self._skip:
            return
        piece = ' '.join(data.split())
        if piece:
            self.text.append(piece)


def parse(html):
    page = _Page()
    page.feed(html)
    return dict(title=' '.join(page.title.split())[:200],
                text='\n'.join(page.text), links=page.links)


class WebReader:
    """op=search/open/follow/read_more over real HTTP, with real artifacts."""

    def __init__(self, core, *, fetch=None, search=None):
        self.core = core
        self.home = core.home
        self._fetch = fetch
        self._search = search

    def available(self):
        return True

    def _pull(self, url, headers=None, warm=None):
        if self._fetch is not None:
            return self._fetch(url)
        import httpx
        # 百度没有 BAIDUID cookie 会直接跳安全验证页，所以先访问一次站点首页。
        with httpx.Client(headers=headers or SEARCH_HEADERS,
                          timeout=20, follow_redirects=True) as client:
            if warm:
                try:
                    client.get(warm)
                except Exception:
                    pass
            response = client.get(url)
            response.raise_for_status()
            return response.text

    def _artifact(self, payload):
        folder = self.home / 'data/companion/artifacts'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (uuid.uuid4().hex + '.web.json')
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        return str(path)

    async def _read(self, url, cursor=0):
        # A slow page must not hold the shared event loop for its timeout.
        html = await asyncio.to_thread(self._pull, url)
        page = parse(html)
        page['links'] = [absolute for link in page['links']
                         if urlsplit(absolute := urljoin(url, link)).scheme in {'http', 'https'}]
        text = page['text']
        chunk = text[cursor:cursor + CHUNK]
        next_cursor = cursor + len(chunk)
        complete = next_cursor >= len(text)
        source = self._artifact(dict(url=url, fetched_at=time.time(), title=page['title'],
                                     text=text, links=page['links']))
        return dict(status='completed', op='read', url=url, title=page['title'],
                    fetched_at=time.time(), text=chunk, cursor=cursor, next_cursor=next_cursor,
                    complete=complete, links=page['links'][:20], source_path=source,
                    read_scope='paragraphs' if not complete else 'whole_page')

    def _search_web(self, query):
        """逐源尝试，只有真的回应了查询的结果才采用；全部不合格就报失败，不给无关内容。"""
        tried = []
        for name, prefix, host, warm in ENGINES:
            url = prefix + quote(query)
            try:
                raw = self._pull(url, SEARCH_HEADERS, warm)
            except Exception as error:
                tried.append('%s:%s' % (name, type(error).__name__))
                continue
            items = parse_results(raw, host)
            if not results_relevant(query, items):
                tried.append('%s:结果与查询无关' % name)
                continue
            source = self._artifact(dict(url=url, fetched_at=time.time(), engine=name, results=items))
            return items, source
        raise ValueError('没有搜索源返回与查询相关的结果（%s）' % ', '.join(tried))

    async def run(self, action):
        op = str(action.get('op') or 'open')
        arguments = action.get('arguments')
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or '{}')
            except ValueError:
                raise ValueError('web arguments 需要 JSON 对象')
        arguments = arguments or {}
        if not isinstance(arguments, dict):
            raise ValueError('web arguments 需要 JSON 对象')
        if op == 'open':
            url = str(arguments.get('url') or '').strip()
            if not url:
                raise ValueError('web open 需要 url')
            return await self._read(url, int(arguments.get('cursor') or 0))
        if op == 'read_more':
            url = str(arguments.get('url') or '').strip()
            if not url:
                raise ValueError('web read_more 需要 url 与 cursor')
            return await self._read(url, int(arguments.get('cursor') or 0))
        if op == 'follow':
            ref = arguments.get('link_ref')
            links = arguments.get('links') or []
            if isinstance(ref, int) and 0 <= ref < len(links):
                return await self._read(urljoin(str(arguments.get('url') or ''), str(links[ref])))
            raise ValueError('web follow 需要有效的 link_ref')
        if op == 'search':
            query = str(arguments.get('query') or '').strip()
            if not query:
                raise ValueError('web search 需要 query')
            try:
                if self._search:
                    results = await asyncio.to_thread(self._search, query)
                    source = self._artifact(dict(query=query, fetched_at=time.time(), results=results))
                else:
                    results, source = await asyncio.to_thread(self._search_web, query)
            except Exception as error:
                return dict(status='failed', op='search', query=query, results=[], complete=False,
                            read_scope='search_snippet', output='搜索未取得有效结果：' + str(error)[:240])
            return dict(status='completed', op='search', query=query, results=results,
                        complete=False, read_scope='search_snippet', source_path=source,
                        note='搜索摘要不是原文；要看内容请用 open 打开具体链接。')
        raise ValueError('不支持的 web 操作：' + op)
