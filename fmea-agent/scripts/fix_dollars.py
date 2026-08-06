#!/usr/bin/env python
# -*- coding: utf-8 -*-
p = r'D:\agent-fmea\fmea-agent\chat.html'
with open(p, 'r', encoding='utf-8') as f:
    c = f.read()

# Fix escaped template literal expressions inside the renderFailure function body
# Only the JS template literal expressions should be unescaped (backslash + dollar → dollar)
# But this is dangerous - we need to only fix within the template literal block

# Find the renderFailure function bounds
start = c.find('function renderFailure(body, data) {')
# Find the matching closing brace - look for clearFailureSearch
end = c.find('function clearFailureSearch()', start)
if start < 0 or end < 0:
    print('ERROR: markers not found')
    exit(1)

before = c[:start]
func = c[start:end]
after = c[end:]

# Inside this function body, fix \$ → $ ONLY in places where it makes sense
# (template literal expressions, not regular JS code)
# Simple approach: replace \\$ with $
fixed = func.replace('\\$', '$')

with open(p, 'w', encoding='utf-8') as f:
    f.write(before + fixed + after)
print(f'Fixed {func.count(chr(92) + "$")} escaped dollar signs')