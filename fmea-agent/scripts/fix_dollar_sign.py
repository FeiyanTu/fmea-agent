#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Fix: In the renderFailure template literal, \$ → $ so JS interpolates correctly"""
p = r'D:\agent-fmea\fmea-agent\chat.html'
with open(p, 'r', encoding='utf-8') as f:
    c = f.read()

# Find the renderFailure function
marker = 'function renderFailure(body, data) {'
start = c.find(marker)
if start < 0:
    print('ERROR: renderFailure not found')
    exit(1)

# Find the end - the next top-level function  
end_marker = '\nfunction renderRiskFileManagerOnly'
end = c.find(end_marker, start)
if end < 0:
    print('ERROR: end marker not found')
    exit(1)

before = c[:start]
func_code = c[start:end]
after = c[end:]

# Inside the template literal (between backticks after body.innerHTML =),
# replace \$ with $
# Find the template literal boundaries
body_html_start = func_code.find('body.innerHTML = `')
if body_html_start < 0:
    print('ERROR: body.innerHTML template not found')
    exit(1)

# From body_html_start+1, find the matching closing backtick
# We need to find the last ` in the function that closes the template
# Skip the one at body_html_start
template_start = body_html_start + len('body.innerHTML = ')
start_char = func_code[template_start]  # should be `
if start_char != '`':
    print(f'ERROR: expected backtick at {template_start}, got {repr(start_char)}')
    exit(1)

# Find the matching closing backtick
# Since there are nested backticks in the template (it's a multi-segment template),
# we need to find which one closes body.innerHTML
# Actually JS template literals backticks aren't nested, but there are \$ escapes
# The closing backtick is at the line with ; } }
# Let's find it differently - find `; } close of the function
# Actually let's just look for the closing backtick pattern
inner_start = template_start + 1
# Look for `;` pattern that closes body.innerHTML
close_pos = func_code.find('`;', inner_start)
if close_pos < 0:
    print('ERROR: template closing not found')
    exit(1)

# The template content between backticks
template_content = func_code[inner_start:close_pos]

# Count \$ before $ in the template content
count = template_content.count('\\$')
print(f'Found {count} escaped dollar signs in template')
# Fix them
template_fixed = template_content.replace('\\$', '$')

# Rebuild
func_fixed = func_code[:body_html_start] + 'body.innerHTML = `' + template_fixed + '`;'
# Check balance of backticks - there might be more template literals later

result = before + func_fixed + after
with open(p, 'w', encoding='utf-8') as f:
    f.write(result)
print('Fixed')
