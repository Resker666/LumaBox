"""Real subprocess fixture, isolated from the external Douyin network."""
import sys
from lumabox.web.worker import JobWorker

WORK_ID = "7686430330920075249"
URL = f"https://www.douyin.com/video/{WORK_ID}"

CHILD = r'''
import hashlib,json,sys,time,zipfile
from pathlib import Path
mode, root, fixture = sys.argv[1:]
root=Path(root)
if mode == 'timeout':
    time.sleep(60)
if mode == 'delayed':
    time.sleep(3)
if mode == 'failed':
    print(json.dumps({'status':'failed','error':'AUTH_REQUIRED','diagnostics':{'reason':'secret'}}))
    sys.exit(1)
if mode in ('CHALLENGE_REQUIRED','RESOURCE_TIMEOUT'):
    print(json.dumps({'status':'failed','error':mode,'diagnostics':{'reason':'private'}}))
    sys.exit(1)
work_id='7686430330920075249'
kind='gallery' if mode in ('gallery','gallery_large') else 'video'
dest=root/(work_id+'-'+'a'*32)
dest.mkdir(parents=True)
data=Path(fixture).read_bytes()
if mode == 'gallery_large':
    data += b'\0' * (20*1024*1024-len(data))  # Storage fixture; no decode claim.
name='01.jpg' if kind == 'gallery' else work_id+'.mp4'
(dest/name).write_bytes(data)
entry={'name':name,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),
       'width':64,'height':48,'duration':None if kind == 'gallery' else .4}
result={'run_id':'a'*32,'status':'success','work_id':work_id,'kind':kind,
 'metadata_parsed':True,'expected_count':1,'actual_count':1,'files':[entry],
 'bytes':len(data),'checks':{'full_decode':True},'headless':False,'http_status':200,
 'output_directory':str(dest),'report_path':str(dest/'report.json')}
if mode == 'gallery_large':
    for index in range(2,11):
        extra={**entry,'name':f'{index:02d}.jpg'}
        (dest/extra['name']).write_bytes(data)
        result['files'].append(extra)
    result.update(expected_count=10,actual_count=10,bytes=10*len(data))
if mode == 'hash': entry['sha256']='0'*64
if mode == 'missing': (dest/name).unlink()
if mode == 'escape': result['output_directory']=str(root.parent)
if mode == 'identity': result['work_id']='1111111111111111111'
if mode == 'decode': result['checks']['full_decode']=False
if mode == 'count': result['expected_count']=2
(dest/'report.json').write_text(json.dumps(result),encoding='utf-8')
if kind == 'gallery':
    with zipfile.ZipFile(dest/(work_id+'-images.zip'),'w') as z:
        for file in result['files']:
            z.write(dest/file['name'],file['name'])
        z.write(dest/'report.json','report.json')
print(json.dumps(result))
'''


class FixtureWorker(JobWorker):
    def __init__(self, config, store, script, fixture, mode="success"):
        super().__init__(config, store)
        self.script, self.fixture, self.mode = script, fixture, mode

    def command(self, job, directory):
        return [sys.executable, str(self.script), self.mode, str(directory), str(self.fixture)]


def fixture_worker(config, store, tmp_path, fixture, mode="success"):
    script = tmp_path / "cli_fixture.py"
    script.write_text(CHILD, encoding="utf-8")
    return FixtureWorker(config, store, script, fixture, mode)
