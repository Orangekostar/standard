"""Create the explicitly requested research PR, preserving actual API receipt.

Credentials are captured internally and never written to a report or stdout.
The tool can operate without gh; API denial is recorded, never called success.
"""
import argparse
import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def token():
    value=os.getenv('GH_TOKEN') or os.getenv('GITHUB_TOKEN')
    if value: return value
    env={**os.environ,'GIT_TERMINAL_PROMPT':'0','GCM_INTERACTIVE':'never'}
    try:
        result=subprocess.run(['git','credential','fill'],input='protocol=https\nhost=github.com\n\n',
            capture_output=True,text=True,env=env,timeout=10)
    except subprocess.TimeoutExpired: return None
    if result.returncode: return None
    fields=dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
    return fields.get('password')


def request(method,url,headers,data=None):
    req=urllib.request.Request(url,method=method,headers=headers,
        data=json.dumps(data).encode() if data is not None else None)
    try:
        with urllib.request.urlopen(req,timeout=30) as response:
            return response.status,json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code,json.loads(exc.read().decode())


def main():
    p=argparse.ArgumentParser();p.add_argument('output',type=Path);p.add_argument('--body-file',type=Path,required=True)
    a=p.parse_args();secret=token()
    headers={'Accept':'application/vnd.github+json','Content-Type':'application/json','User-Agent':'quant-high-win-research',
             'X-GitHub-Api-Version':'2022-11-28'}
    if secret: headers['Authorization']='Bearer '+secret
    api='https://api.github.com/repos/Orangekostar/standard/pulls'
    status,found=request('GET',api+'?state=open&head='+urllib.parse.quote('Orangekostar:research/mainboard-high-win-v1'),headers)
    reused=False
    if status==200 and found:
        response=found[0];reused=True
    else:
        status,response=request('POST',api,headers,dict(title='沪深主板高胜率策略实验室：16候选与252账户验证',
            head='research/mainboard-high-win-v1',base='fix/mainboard-only-universe',body=a.body_file.read_text()))
    receipt=dict(at=datetime.now(timezone.utc).isoformat(),http_status=status,reused_existing=reused,
        authenticated=bool(secret),created_or_found=status in (200,201) and bool(response.get('number')),
        number=response.get('number'),url=response.get('html_url'),message=response.get('message'),
        documentation_url=response.get('documentation_url'),errors=response.get('errors'))
    (a.output/'pr_api_receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(receipt,ensure_ascii=False))


if __name__=='__main__': main()
