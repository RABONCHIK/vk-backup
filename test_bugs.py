import importlib, vk_backup
importlib.reload(vk_backup)
v = vk_backup
from pathlib import Path
import tempfile, shutil, urllib.parse
from html import escape

ok = True
def chk(cond, name, detail=''):
    global ok
    status = 'OK  ' if cond else 'FAIL'
    print(f'  {status} {name}' + (f'  [{detail}]' if detail and not cond else ''))
    if not cond:
        ok = False

print('=== REGRESSION TESTS ===')
print()

tmp = Path(tempfile.mkdtemp())

# Fake downloader
def fake_dl(url, dest, retries=3):
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b'fake')
    return True
v.download_file = fake_dl

# Fake API (for video.get)
def fake_api_video(method, **params):
    if method == 'video.get':
        return {'count': 1, 'items': [{'files': {'mp4_360': 'http://fake/v.mp4'}}]}
    raise RuntimeError(f'unexpected: {method}')

print('BUG1: double-escape in doc')
att = {'type': 'doc', 'file': 'doc.pdf', 'url': 'http://x.com', 'text': 'File & doc', 'duration': 0}
html = v.att_to_html(att, 'media')
chk('&amp;amp;' not in html, 'no double &amp;amp;', html)
chk('File &amp; doc' in html, 'correct single &amp;', html)

print()
print('BUG2: empty ext on doc')
r = v.process_attachment(
    {'type': 'doc', 'doc': {'id': 1, 'owner_id': 2, 'title': 'test', 'ext': '', 'url': 'http://x.com/a'}},
    tmp
)
chk(r['file'] is not None, 'file created with empty ext', str(r['file']))
chk((r['file'] or '').endswith('.bin'), 'extension is .bin', str(r['file']))
chk(not (r['file'] or '').endswith('.'), 'no trailing dot', str(r['file']))

print()
print('BUG3: _processed_atts leak in nested fwd_messages')
msg_fwd = [
    {
        '_processed_atts': [{'type': 'sticker'}],
        'text': 'fwd1', 'attachments': [],
        'fwd_messages': [
            {'_processed_atts': [{'type': 'audio_message'}], 'text': 'fwd2', 'fwd_messages': [], 'attachments': []}
        ]
    }
]
import copy
cleaned = copy.deepcopy(msg_fwd)
v._clean_fwd(cleaned)
has_leak = any('_processed_atts' in f for f in cleaned)
has_deep_leak = any('_processed_atts' in f2 for f in cleaned for f2 in f.get('fwd_messages', []))
chk(not has_leak, 'level1 fwd cleaned')
chk(not has_deep_leak, 'level2 fwd cleaned')

print()
print('BUG4: href uses url-encoding not html-escaping')
folder = '444_Ivan & Test'
folder_href = urllib.parse.quote(folder, safe='_-.')
chk('&amp;' not in folder_href, 'no &amp; in href', folder_href)
chk('%26' in folder_href, '& properly percent-encoded', folder_href)

print()
print('BUG5: doc with normal ext works')
r2 = v.process_attachment(
    {'type': 'doc', 'doc': {'id': 5, 'owner_id': 6, 'title': 'Contract', 'ext': 'pdf', 'url': 'http://x/a.pdf'}},
    tmp
)
chk(r2['file'] is not None, 'file created', str(r2['file']))
chk((r2['file'] or '').endswith('.pdf'), 'has .pdf extension', str(r2['file']))

print()
print('EXTRA: video without direct link uses video.get')
v.api = fake_api_video
r3 = v.process_attachment(
    {'type': 'video', 'video': {'id': 99, 'owner_id': 88, 'access_key': 'xyz', 'title': 'Krug'}},
    tmp
)
chk(r3['file'] in ('video_88_99.mp4', 'videos/video_88_99.mp4'), 'video downloaded via video.get', str(r3['file']))
chk(r3['url'] == 'https://vk.com/video88_99', 'url correct', r3['url'])

print()
print('EXTRA: render_fwd without _processed_atts does not crash')
fwd_no_att = [{'from_id': 1, 'text': 'hello world', 'fwd_messages': []}]
html_fwd = v.render_fwd(fwd_no_att, {}, 'media')
chk('hello world' in html_fwd, 'text rendered', html_fwd[:80])

print()
print('EXTRA: token extraction from full URL')
url_full = 'https://oauth.vk.com/blank.html#access_token=vk1.abc123&expires_in=0'
token = url_full.split('access_token=')[1].split('&')[0]
chk(token == 'vk1.abc123', 'token extracted from URL', token)

print()
print('EXTRA: safe_name edge cases')
chk(v.safe_name('') == 'unnamed', 'empty string -> unnamed')
chk(v.safe_name('...') == 'unnamed', 'dots only -> unnamed')
chk(v.safe_name('A/B:C*D') == 'A_B_C_D', 'special chars replaced', v.safe_name('A/B:C*D'))
chk(len(v.safe_name('x' * 100)) == 60, 'max length 60')

shutil.rmtree(tmp)

print()
print('=' * 40)
print('RESULT:', 'ALL PASSED' if ok else 'FAILURES FOUND')
import sys
sys.exit(0 if ok else 1)
