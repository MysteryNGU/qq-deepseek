if start < 0:
    raise SystemExit('probe heredoc not found')
start = src.index('\n', start) + 1
end = src.find('\nPYEOF', start)
body = src[start:end]
# 写盘时严格要求 UTF-8/LF，避免 shell heredoc 把中文截断
target.write_text(body + '\n', encoding='utf-8')
print('wrote', target, len(body))
