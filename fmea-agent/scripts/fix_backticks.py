#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Fix escaped backticks left by the previous script"""
p = r'D:\agent-fmea\fmea-agent\chat.html'
with open(p, 'r', encoding='utf-8') as f:
    c = f.read()

# Check current state
for keyword in ['\\`', '`', '\\${', '$']:
    cnt = c.count(keyword)
    print(f'  {repr(keyword)}: {cnt} occurrences')

# The issue: Python raw string `\`` in the script produced literal `\`` in output
# We need `\`` → `` ` ``
c = c.replace('\\`', '`')

with open(p, 'w', encoding='utf-8') as f:
    f.write(c)
print('Fixed backtick escaping')
