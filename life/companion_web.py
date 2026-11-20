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

from .companion_work import (PROVENANCE_DIRECT, PROVENANCE_TOOL, missing, provenance,
                             visibility)

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
    return _parse_results(raw, host)[0]


def _parse_results(raw, host):
    """返回 ``(items, filtered, dropped)``：被丢掉的条目按原因计数，并留下标题/URL。

    以前被丢掉的东西（导航块、引擎自己的链接、聚合/验证码页）是静默消失的，
    回执上只剩「0 条」；现在「被过滤多少、为什么」连条目的标题与 URL 一起落进
    artifact——她可以自己核对引擎到底给了什么、被拿掉的是不是真该拿掉。
    """
    items = []
    filtered = {'no_title': 0, 'bad_link': 0, 'nav': 0, 'self_link': 0, 'blocked': 0}
    dropped = []

    def drop(reason, title='', url=''):
        filtered[reason] += 1
        if len(dropped) < 40:      # artifact 不吞浏览结果，但也不为废块撑爆
            dropped.append(dict(reason=reason, title=str(title or '')[:120],
                                url=str(url or '')[:300]))

    for part in re.split(r'<h3\b', raw, flags=re.I)[1:]:
        block = '<h3' + part
        title = re.search(r'<h3[^>]*>(.*?)</h3>', block, re.S)
        href = re.search(r'<a[^>]+href="([^"]+)"', block)
        if not title or not href:
            drop('no_title')
            continue
        text = clean_text(title.group(1))
        link = href.group(1)
        if not text or not link.startswith(('http', '/')):
            drop('bad_link', text, link)
            continue
        url = urljoin(host, link)
        if any(nav in text for nav in NAV_TITLES):
            drop('nav', text, url)
            continue
        # 引擎自己的导航/“再搜一次”链接不是搜索结果；跳转链接保留，open 会跟随它。
        if url.startswith(host) and '/link?' not in url:
            drop('self_link', text, url)
            continue
        if any(blocked in url for blocked in BLOCKED_URLS):
            drop('blocked', text, url)
            continue
        snippet = block[title.end():title.end() + 6000]
        snippet = re.sub(r'<(script|style)\b.*?</\1>', ' ', snippet, flags=re.S | re.I)
        snippet = re.sub(r'<[^>]*$', ' ', snippet)   # 窗口可能截在标签中间
        snippet = clean_text(snippet)[:200]
        items.append(dict(title=text[:120], url=url, snippet=snippet))
        if len(items) >= 12:
            break
    return items, filtered, dropped


def item_relevance(query, items):
    """逐条标「标题里有没有查询词」。只标事实，不替她定相关性。"""
    terms = [term for term in re.split(r'\s+', query) if len(term) >= 2]
    for item in items:
        if not isinstance(item, dict):
            continue
        item['relevant'] = bool(terms) and any(term in str(item.get('title') or '')
                                               for term in terms)
    return items


def results_relevant(query, items):
    """（保留）整批是否「够相关」。**不再用它做门禁**：相关性由她判断。"""
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
        gaps, not_obtained = [], []
        if not complete:
            gaps.append(dict(what='页面正文', reason='limit', reason_text='超出上限',
                             detail='这次只带回第 %d-%d 个字符（全页 %d 字）；用 read_more 带 cursor=%d 接着读'
                                    % (cursor, next_cursor, len(text), next_cursor),
                             shown_chars=len(chunk), total_chars=len(text),
                             next_cursor=next_cursor))
        if not text.strip():
            not_obtained.append(missing('页面正文', 'unavailable',
                                        '抓到的 HTML 里没有可读正文（可能是脚本渲染的页面，'
                                        '或站点返回了空壳/验证页）；标题=%r' % page['title'][:80]))
        return dict(status='completed', op='read', url=url, title=page['title'],
                    fetched_at=time.time(), text=chunk, cursor=cursor, next_cursor=next_cursor,
                    complete=complete, links=page['links'][:20], source_path=source,
                    read_scope='whole_page' if complete else 'paragraphs',
                    chars=len(chunk), chars_total=len(text),
                    **provenance(PROVENANCE_DIRECT, source='web.read',
                                 note='这是这个地址返回的页面正文（抓到的 HTML 去标签后的原文）；'
                                      '「只读了前 N 字」这类计数是工具自己的报告。',
                                 parts={'text': PROVENANCE_DIRECT, 'coverage': PROVENANCE_TOOL}),
                    visibility=visibility(truncated=gaps, not_obtained=not_obtained))

    def _fetch_failure(self, op, url, error):
        """打不开就是打不开：分层写原因，别只留一个 status。"""
        text = '%s: %s' % (type(error).__name__, str(error)[:300])
        lowered = text.lower()
        if any(word in lowered for word in ('timeout', 'timed out', 'connect', 'dns', 'network')):
            reason = 'unavailable'
        elif any(word in lowered for word in ('401', '403', 'forbidden', 'unauthorized', 'login')):
            reason = 'unauthorized'
        else:
            reason = 'failed'
        return dict(status='failed', op=op, url=str(url), output='这次没有取到页面：' + text,
                    **provenance(PROVENANCE_TOOL, source='web.%s' % op,
                                 note='这是抓取失败的报告，不是页面内容。'),
                    visibility=visibility(not_obtained=[
                        missing('页面 %s' % url, reason, text)]))

    def _search_web(self, query):
        """逐源尝试，把每个引擎的真实情况带回来；不再替她判断相关性。

        以前：任何一个引擎的结果里标题没有足够查询词，整批结果连同「引擎返回了
        什么」一起被丢掉，只报一句「没有搜索源返回与查询相关的结果」。那是拿一个
        关键词门槛替她做判断，而且让她连原始条目都看不见。
        现在：条目原样交回（标 relevant 的只是标题里出现查询词），每个引擎的成功/
        失败/被过滤条数逐项报告，原文落一份 artifact。
        """
        engines, results, seen = [], [], set()
        duplicates, capped = [], []
        for name, prefix, host, warm in ENGINES:
            url = prefix + quote(query)
            record = dict(engine=name, url=url, status='failed', items=0, relevant=0,
                          filtered={}, filtered_total=0, dropped=[], error='')
            try:
                raw = self._pull(url, SEARCH_HEADERS, warm)
            except Exception as error:  # noqa: BLE001 - 每个源的失败原因都要原样带回
                record['error'] = '%s: %s' % (type(error).__name__, str(error)[:200])
                engines.append(record)
                continue
            items, filtered, dropped = _parse_results(raw, host)
            item_relevance(query, items)
            record.update(status='ok' if items else 'empty', items=len(items),
                          relevant=sum(1 for item in items if item.get('relevant')),
                          filtered=filtered, filtered_total=sum(filtered.values()),
                          dropped=dropped)
            for item in items:
                item['engine'] = name
                if item['url'] in seen:
                    # 跨引擎同 URL：去掉重复，但把「去掉了哪条」留下来，不静默消失。
                    if len(duplicates) < 40:
                        duplicates.append(dict(title=item['title'], url=item['url'],
                                               engine=name, reason='duplicate_url'))
                    continue
                seen.add(item['url'])
                results.append(item)
            engines.append(record)
        # 标题里出现查询词的排前面，但没出现的也照样交回：不替她筛掉。
        results.sort(key=lambda item: 0 if item.get('relevant') else 1)
        for item in results[24:]:
            if len(capped) < 40:
                capped.append(dict(title=item.get('title'), url=item.get('url'),
                                   engine=item.get('engine'), reason='over_limit'))
        results = results[:24]
        filtered_entries = [dict(entry, engine=row['engine'])
                            for row in engines for entry in row.get('dropped') or []]
        steps = []
        if any(row['status'] == 'ok' for row in engines):
            # 去重哪怕 0 条也写出来：她要知道「这批发回的结果被怎么处理过」。
            steps.append('跨引擎同 URL 去重 %d 条' % len(duplicates))
        if capped:
            steps.append('按「标题里是否出现查询词」排序后最多给 24 条，砍掉 %d 条（完整见 artifact）'
                         % len(capped))
        elif results:
            steps.append('结果按「标题里是否出现查询词」排序（命中在前），没命中的也保留')
        if filtered_entries:
            steps.append('被过滤的条目（导航/引擎自身链接/聚合页）连标题与 URL 落进 artifact')
        coverage = dict(engines=engines, tried=len(engines),
                        succeeded=sum(1 for row in engines if row['status'] == 'ok'),
                        empty=sum(1 for row in engines if row['status'] == 'empty'),
                        failed=sum(1 for row in engines if row['status'] == 'failed'),
                        items=len(results),
                        filtered_total=sum(row.get('filtered_total') or 0 for row in engines),
                        duplicates=len(duplicates), capped=len(capped),
                        relevant=sum(1 for item in results if item.get('relevant')))
        payload = dict(query=query, fetched_at=time.time(), engines=engines,
                       results=results, coverage=coverage,
                       filtered_entries=filtered_entries,
                       duplicates=duplicates, capped=capped, steps=steps)
        return dict(results=results, coverage=coverage, steps=steps,
                    source_path=self._artifact(payload))

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
        if op in {'open', 'read_more', 'follow'}:
            if op == 'follow':
                ref = arguments.get('link_ref')
                links = arguments.get('links') or []
                if not (isinstance(ref, int) and 0 <= ref < len(links)):
                    raise ValueError('web follow 需要有效的 link_ref')
                url = urljoin(str(arguments.get('url') or ''), str(links[ref]))
                cursor = 0
            else:
                url = str(arguments.get('url') or '').strip()
                if not url:
                    raise ValueError('web %s 需要 url' % op)
                cursor = int(arguments.get('cursor') or 0)
            try:
                return await self._read(url, cursor)
            except ValueError:
                raise
            except Exception as error:  # noqa: BLE001 - 打不开要分层写原因
                return self._fetch_failure(op, url, error)
        if op == 'search':
            query = str(arguments.get('query') or '').strip()
            if not query:
                raise ValueError('web search 需要 query')
            try:
                if self._search:
                    items = list(await asyncio.to_thread(self._search, query) or [])
                    item_relevance(query, items)
                    coverage = dict(engines=[dict(engine='injected', url='', status='ok',
                                                  items=len(items), relevant=sum(
                                                      1 for item in items if item.get('relevant')),
                                                  filtered={}, filtered_total=0, error='')],
                                    tried=1, succeeded=1, empty=0, failed=0,
                                    items=len(items), filtered_total=0,
                                    relevant=sum(1 for item in items if item.get('relevant')))
                    source = self._artifact(dict(query=query, fetched_at=time.time(),
                                                 results=items, coverage=coverage))
                    found = dict(results=items, coverage=coverage, source_path=source,
                                 steps=['由调用方注入的搜索源：不经过跨引擎去重/排序'])
                else:
                    found = await asyncio.to_thread(self._search_web, query)
            except Exception as error:  # noqa: BLE001 - 失败原因分层带回
                detail = '%s: %s' % (type(error).__name__, str(error)[:300])
                return dict(status='failed', op='search', query=query, results=[],
                            complete=False, read_scope='search_snippet', coverage=dict(engines=[]),
                            output='这次没有取得搜索结果：' + detail,
                            **provenance(PROVENANCE_TOOL, source='web.search',
                                         note='这是搜索工具对失败的报告，不是引擎的原始结果。'),
                            visibility=visibility(not_obtained=[
                                missing('搜索结果', 'unavailable', detail)]))
            results = list(found.get('results') or [])
            coverage = dict(found.get('coverage') or {})
            steps = [str(item) for item in (found.get('steps') or []) if str(item).strip()]
            notes = ('搜索摘要不是原文；要看内容请用 open 打开具体链接。'
                     '结果是各引擎原样返回的条目：标 relevant 的只是标题里出现查询词，'
                     '没标的不等于不相关，相关性交给你判断。')
            if steps:
                # 去重/排序/截断这三步是工具做的，如实写出来，别让她以为引擎原样就长这样。
                notes += '工具对这批发回的结果做的处理：' + '；'.join(steps) + '。'
            gap = []
            status = 'completed'
            if not results:
                detail = '；'.join('%s=%s(%s)' % (row.get('engine'), row.get('status'),
                                                 row.get('error') or '0 条')
                                   for row in coverage.get('engines') or []) or '没有可用的引擎'
                gap.append(missing('搜索结果', 'failed', '每个引擎这次都没有返回可用条目：' + detail))
                # 每个引擎都失败时这是「没搜到」而不是「搜到了空」：状态也要这样说。
                if coverage.get('tried') and coverage.get('failed') == coverage.get('tried'):
                    status = 'failed'
            primary = PROVENANCE_DIRECT if status == 'completed' else PROVENANCE_TOOL
            if status == 'completed':
                origin_note = ('条目来自引擎返回页面的原文；每个引擎成功/失败/被过滤多少条'
                               '是工具自己的统计，失败原因是引擎或网络的原始报错。')
            else:
                origin_note = '这次没有条目可给：下面写的是每个引擎各自的失败原因（引擎或网络的原始报错）。'
            return dict(status=status, op='search', query=query, results=results,
                        complete=False, read_scope='search_snippet', coverage=coverage,
                        relevant_count=coverage.get('relevant') or 0,
                        source_path=found.get('source_path'), note=notes,
                        output='（%d 条，来自 %d 个引擎：%s）' % (
                            len(results), coverage.get('tried') or 0,
                            '；'.join('%s %s %d 条，过滤 %d 条' % (
                                row.get('engine'), row.get('status') or '?',
                                row.get('items') or 0, row.get('filtered_total') or 0)
                                for row in coverage.get('engines') or []) or '无'),
                        **provenance(primary, source='web.search', note=origin_note,
                                     parts={'results': PROVENANCE_DIRECT,
                                            'coverage': PROVENANCE_TOOL}),
                        visibility=visibility(not_obtained=gap, note=notes))
        raise ValueError('不支持的 web 操作：' + op)
