#!/usr/bin/env python
# -*- coding: utf-8 -*-
p = r'D:\agent-fmea\fmea-agent\chat.html'
with open(p, 'r', encoding='utf-8') as f:
    c = f.read()

# Check what's happening
import re
backslash_bt = '\\`' 
count = c.count(backslash_bt)
print(f'Escaped backticks (backslash + backtick): {count}')

if count > 0:
    c = c.replace(backslash_bt, '`')
    with open(p, 'w', encoding='utf-8') as f:
        f.write(c)
    print('Fixed')
else:
    print('No escaped backticks found')
    
# Also verify template literal syntax
# Check for $${ usage which should be just ${ 
dollar_brace = c.count('$${')
if dollar_brace > 0:
    c = c.replace('$${', '${')
    with open(p, 'w', encoding='utf-8') as f:
        f.write(c)
    print(f'Fixed {dollar_brace} double-dollar')
