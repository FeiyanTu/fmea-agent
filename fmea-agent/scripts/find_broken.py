#!/usr/bin/env python
# -*- coding: utf-8 -*-
import re
p = r'D:\agent-fmea\fmea-agent\chat.html'
with open(p, 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Find escaped dollar signs
escaped = []
for i, line in enumerate(lines, 1):
    if '\\${esc' in line:
        escaped.append((i, line.rstrip()[:120]))

print(f'Found {len(escaped)} lines with \\${{esc...')
for i, line in escaped[:3]:
    print(f'  Line {i}: {line}')