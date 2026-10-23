"""Shared patch matching used by the native DSH workshop."""
import difflib
import hashlib
from pathlib import Path

class Workshop:
    @staticmethod
    def _sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    # ---------------------------------------------------------- 找到要改的那段
    @staticmethod
    def _variants(old: str) -> list[str]:
        """她能抄出来的几种变体：行尾空白、CRLF、以及多转义了一层反斜杠。"""
        NL = chr(10)
        BACK = chr(92)
        cands: list[str] = []
        for o in (old, old.replace(chr(13) + NL, NL)):
            cands.append(o)
            cands.append(NL.join(ln.rstrip() for ln in o.splitlines()))
            fixed = o.replace(BACK + BACK, BACK)
            cands.append(fixed)
            cands.append(NL.join(ln.rstrip() for ln in fixed.splitlines()))
        out: list[str] = []
        for o in cands:
            if o and o not in out:
                out.append(o)
        return out

    def _locate(self, src: str, old: str) -> tuple[int | None, int | None, str, str]:
        """返回 (start, end, 怎么匹配上的, 最接近的原文)。找不到时 start 为 None。"""
        NL = chr(10)
        if not old:
            return None, None, "", "没给 old 原文"
        for cand in self._variants(old):
            if src.count(cand) == 1:
                i = src.index(cand)
                return i, i + len(cand), "精确匹配", ""
        # 按行比（忽略行尾空白）：抄错几个空格也能改
        src_lines = src.splitlines()
        want = [ln.rstrip() for ln in old.replace(chr(13) + NL, NL).splitlines()]
        while want and not want[0].strip():
            want.pop(0)
        while want and not want[-1].strip():
            want.pop()
        if want and len(want) <= len(src_lines):
            hits = [i for i in range(len(src_lines) - len(want) + 1)
                    if [x.rstrip() for x in src_lines[i:i + len(want)]] == want]
            if len(hits) == 1:
                i = hits[0]
                start = sum(len(x) + 1 for x in src_lines[:i])
                end = start + sum(len(x) + 1 for x in src_lines[i:i + len(want)])
                return start, min(end, len(src)), "按行匹配", ""
            if len(hits) > 1:
                return None, None, f"这段在文件里出现了 {len(hits)} 次", ""
        # 实在对不上：把最接近的一段原文给她，让她照抄
        best = ""
        try:
            sm = difflib.SequenceMatcher(None, src_lines, want)
            block = max(sm.get_matching_blocks(), key=lambda b: b.size, default=None)
            if block and block.size >= 2:
                best = NL.join(src_lines[block.a:block.a + max(block.size, len(want))])
        except Exception:
            best = ""
        return None, None, "对不上", best


    # ---------------------------------------------------------------- 快照与补丁
