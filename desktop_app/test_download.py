import hashlib
import http.server
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from core import Engine, detect_model_type, parse_hf_url, file_page_url, file_url, output_path, validate_proxy, resolve_file, list_repo, remove_completed_identity, save_json
import app as app_module
from app import APP_VERSION, App, DaemonWorkerPool, comfy_repo_target, data_directory, destination_conflicts, ensure_download_capacity, extract_hf_urls, fit_window_rect, normalize_comfy_favorite, normalize_queue_payload, normalize_settings, original_page_url, proportional_right_widths, resolve_destination_plan, validate_target_path

class UrlTests(unittest.TestCase):
    def test_version_and_pasted_links_are_normalized(self):
        self.assertRegex(APP_VERSION,r'^\d+\.\d+\.\d+$')
        first='https://huggingface.co/a/b/resolve/main/model.safetensors?download=true'
        second='https://huggingface.co/c/d'
        text=f'链接：{first}，重复 {first}。Markdown：[{second}]({second})！'
        self.assertEqual(extract_hf_urls(text),[first,second])

    def test_invalid_settings_fall_back_without_blocking_startup(self):
        warnings=[]
        settings=normalize_settings({'default_folder':'yes','folder':None,'proxy':42,'parallel':'abc',
                                     'connections':99,'comfy_root':[], 'diffusion_dir':'other',
                                     'comfy_favorites':'loras','input_rows':100},warnings)
        self.assertTrue(settings['default_folder'])
        self.assertEqual(settings['parallel'],3)
        self.assertEqual(settings['connections'],8)
        self.assertEqual(settings['comfy_root'],'')
        self.assertEqual(settings['diffusion_dir'],'diffusion_models')
        self.assertEqual(settings['comfy_favorites'],[])
        self.assertEqual(settings['input_rows'],18)
        self.assertNotIn('proxy',settings)
        self.assertTrue(warnings)

    def test_same_name_choices_keep_overwrite_or_skip(self):
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'model.safetensors'
            planned=[('first',target),('second',target)]
            self.assertEqual(len(destination_conflicts(planned,set())),1)
            self.assertEqual(resolve_destination_plan(planned,set(),'overwrite'),[('second',target,False)])
            self.assertEqual(resolve_destination_plan(planned,set(),'skip'),[('first',target,False)])
            kept=resolve_destination_plan(planned,set(),'keep')
            self.assertEqual([item[1].name for item in kept],['model.safetensors','model (2).safetensors'])
            target.write_bytes(b'old model')
            self.assertEqual(resolve_destination_plan(planned,set(),'overwrite'),[('second',target,True)])
            self.assertEqual(resolve_destination_plan(planned,set(),'skip'),[])
            self.assertEqual([item[1].name for item in resolve_destination_plan(planned,set(),'keep')],
                             ['model (2).safetensors','model (3).safetensors'])
            self.assertEqual(target.read_bytes(),b'old model')
            with self.assertRaises(ValueError):resolve_destination_plan(planned,{str(target).casefold()},'overwrite')

    def test_overwrite_keeps_original_until_download_completes(self):
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'model.bin';target.write_bytes(b'old model')
            engine=object.__new__(Engine)
            calls=[]
            engine.call=lambda method,*args:calls.append((method,args)) or 'new-gid'
            job={'path':str(target),'url':'https://huggingface.co/a/b/resolve/main/model.bin',
                 'connections':8,'replace_existing':True}
            self.assertEqual(engine.add(job),'new-gid')
            self.assertEqual(target.read_bytes(),b'old model')
            self.assertEqual(calls[0][0],'addUri')
            self.assertEqual(calls[0][1][1]['out'],'model.bin.hfdownload')
            target.with_name('model.bin.hfdownload').write_bytes(b'new model')
            target.with_name('model.bin.hfdownload').replace(target)
            self.assertEqual(target.read_bytes(),b'new model')

    def test_folder_columns_resize_right_side_proportionally(self):
        widths=[300,400,200]
        result=proportional_right_widths(widths,0,400,160)
        self.assertEqual(result,[400,333,167])
        self.assertEqual(proportional_right_widths(widths,1,500,160),[300,440,160])
        self.assertEqual(proportional_right_widths(widths,0,800,160),[580,160,160])
        self.assertEqual(proportional_right_widths(widths,2,500,160),widths)

    def test_popup_stays_inside_the_monitor_that_received_the_click(self):
        left_monitor=(-1920,0,0,1040)
        self.assertEqual(fit_window_rect(-10,230,204,172,left_monitor),(-212,230,204,172))
        self.assertEqual(fit_window_rect(-2100,980,204,172,left_monitor),(-1912,860,204,172))
        right_monitor=(1920,-200,3840,1040)
        self.assertEqual(fit_window_rect(3820,-260,300,240,right_monitor),(3532,-192,300,240))

    def test_oversized_dialog_is_reduced_to_monitor_work_area(self):
        self.assertEqual(fit_window_rect(-2500,-500,2400,1400,(-1920,0,0,1040),20),
                         (-1900,20,1880,1000))

    def test_file_and_repo(self):
        info=parse_hf_url('https://huggingface.co/Comfy-Org/MiniMax-H3/blob/main/vae/model.safetensors?download=true')
        self.assertEqual(info['filename'],'vae/model.safetensors')
        self.assertEqual(parse_hf_url('https://huggingface.co/Comfy-Org/MiniMax-H3')['filename'],'')
        self.assertEqual(parse_hf_url('https://huggingface.co/datasets/a/b/resolve/main/x.parquet')['kind'],'dataset')
        folder=parse_hf_url('https://huggingface.co/Comfy-Org/MiniMax-H3/tree/main/vae')
        self.assertEqual(folder['filename'],'')
        self.assertEqual(folder['folder'],'vae')
        self.assertEqual(parse_hf_url('https://huggingface.co/a/b/tree/dev/a/b')['folder'],'a/b')
        self.assertEqual(parse_hf_url('https://huggingface.co/tree/model')['repo'],'tree/model')
        self.assertEqual(parse_hf_url('https://huggingface.co/tree/model/resolve/main/file.bin')['repo'],'tree/model')
        for reserved_name in ('tree','blob','resolve'):
            self.assertEqual(parse_hf_url('https://huggingface.co/author/'+reserved_name)['repo'],'author/'+reserved_name)
        self.assertEqual(file_page_url(info),'https://huggingface.co/Comfy-Org/MiniMax-H3/blob/main/vae/model.safetensors')
        self.assertEqual(detect_model_type('unet/model.safetensors'),'UNet / 扩散模型')
        self.assertEqual(detect_model_type('text_encoders/t5xxl.safetensors'),'CLIP / 文本编码器')
        self.assertEqual(detect_model_type('loras/style.safetensors'),'LoRA')
        self.assertEqual(detect_model_type('transformer/weights.safetensors'),'UNet / 扩散模型')
        self.assertEqual(detect_model_type('lora/weights.safetensors'),'LoRA')
        self.assertEqual(detect_model_type('transformer/portrait-lora-r128.safetensors'),'LoRA')
        self.assertEqual(detect_model_type('weights-controlnet-xl.safetensors'),'ControlNet')
        viggle=parse_hf_url('https://huggingface.co/Viggle/Qwen-Image-2.1-viggle-turbo/resolve/main/Qwen-Image-2.1-viggle-turbo-v0.3-6step-lora-r128.safetensors?download=true')
        self.assertEqual(detect_model_type(viggle['filename']),'LoRA')
        self.assertEqual(detect_model_type('lorawan/model.safetensors'),'其他')
        self.assertEqual(detect_model_type('misc/readme.md'),'其他')

    def test_unsafe_paths_and_hosts(self):
        for url in ('https://evil.example/a/b/resolve/main/file','https://huggingface.co/a/b/resolve/main/%2e%2e/file','https://huggingface.co/a/b/resolve/main/a%5Cb'):
            with self.assertRaises(ValueError):parse_hf_url(url)
        with self.assertRaises(ValueError):output_path('.',{'repo':'a/b','filename':'../../escape'})
        self.assertEqual(validate_proxy('127.0.0.1:10808'),'http://127.0.0.1:10808')
        with self.assertRaises(ValueError):validate_proxy('http://127.0.0.1:10808/?unexpected=true')

    def test_original_page_keeps_the_user_revision(self):
        info=parse_hf_url('https://huggingface.co/owner/model/resolve/main/model.bin')
        job=dict(info,source_url=file_url(info),revision='a'*40)
        self.assertEqual(original_page_url(job),'https://huggingface.co/owner/model/blob/main/model.bin')

    def test_repository_files_can_share_one_local_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            info={'repo':'author/model','filename':'subfolder/model.safetensors'}
            self.assertEqual(output_path(directory,info),Path(directory)/'author--model'/'subfolder'/'model.safetensors')
            self.assertEqual(output_path(directory,info,flatten=True),Path(directory)/'author--model'/'model.safetensors')
            self.assertEqual(output_path(directory,info,flatten=True,repository_folder=False),
                             Path(directory)/'model.safetensors')
            self.assertEqual(output_path(directory,info,repository_folder=False),
                             Path(directory)/'subfolder'/'model.safetensors')
            with self.assertRaises(ValueError):output_path(directory,dict(info,filename='../model.safetensors'),flatten=True)

    def test_comfy_favorites_must_stay_relative_to_models(self):
        self.assertEqual(normalize_comfy_favorite('loras/people'),'loras/people')
        for unsafe in ('/Windows','C:/Windows','../outside','loras/../../outside','bad:name'):
            self.assertIsNone(normalize_comfy_favorite(unsafe))

    def test_comfy_repo_save_modes_and_scoped_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            models=Path(directory)/'models';chosen=models/'loras'/'Qwen';chosen.mkdir(parents=True)
            repo={'repo':'owner/Turbo-LoRA','folder':'weights'}
            file_info={'filename':'weights/sub/model.safetensors'}
            self.assertEqual(comfy_repo_target(models,repo,file_info,'loras/Qwen'),
                             chosen/'Turbo-LoRA'/'sub'/'model.safetensors')
            self.assertEqual(comfy_repo_target(models,repo,file_info,'loras/Qwen',flatten=True),
                             chosen/'model.safetensors')
            with self.assertRaises(ValueError):
                comfy_repo_target(models,repo,file_info,'../outside')

    def test_resume_identity_is_removed_only_after_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            final=Path(directory)/'model.safetensors'
            partial=Path(str(final)+'.hfdownload')
            identity=Path(str(final)+'.hfdownload.json')
            partial.write_bytes(b'partial')
            identity.write_text('{"url":"example"}',encoding='utf-8')
            self.assertTrue(remove_completed_identity(final))
            self.assertTrue(identity.exists())
            partial.rename(final)
            self.assertTrue(remove_completed_identity(final))
            self.assertFalse(identity.exists())

    def test_json_backup_recovers_a_broken_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);queue=base/'queue.json'
            save_json(queue,[{'id':'old'}],keep_backup=True)
            save_json(queue,[{'id':'new'}],keep_backup=True)
            queue.write_text('{broken',encoding='utf-8')
            app=object.__new__(App);app.data=base;app.load_warnings=[]
            self.assertEqual(App.load(app,'queue.json',[]),[{'id':'old'}])
            self.assertTrue(app.load_warnings)

    def test_unknown_model_gets_default_comfy_recommendation(self):
        class Value:
            def get(self):return 'diffusion_models'
        app=object.__new__(App);app.diffusion_dir=Value()
        self.assertEqual(App.comfy_preferences(app,{'filename':'weights.safetensors'})[0],'diffusion_models')

    def test_comfy_batch_only_locates_a_shared_recognized_type(self):
        class Value:
            def get(self):return 'diffusion_models'
        app=object.__new__(App);app.diffusion_dir=Value()
        lora={'filename':'loras/style.safetensors'}
        second_lora={'filename':'portrait-lora-r128.safetensors'}
        vae={'filename':'vae/decoder.safetensors'}
        unknown={'filename':'NOTICE'}
        self.assertEqual(App.comfy_batch_preferences(app,[lora])[0],'loras')
        self.assertEqual(App.comfy_batch_preferences(app,[lora,second_lora])[0],'loras')
        self.assertEqual(App.comfy_batch_preferences(app,[lora,vae]),())
        self.assertEqual(App.comfy_batch_preferences(app,[lora,unknown]),())
        self.assertEqual(App.comfy_batch_preferences(app,[unknown,unknown]),())

    def test_old_queue_format_is_migrated_and_invalid_rows_are_skipped(self):
        warnings=[]
        jobs=normalize_queue_payload([
            {'repo':'owner/model','filename':'model.safetensors','path':'C:\\models\\model.safetensors','connections':'16','status':'active'},
            {'id':'broken'},
        ],warnings)
        self.assertEqual(len(jobs),1)
        self.assertEqual(jobs[0]['connections'],16)
        self.assertEqual(jobs[0]['revision'],'main')
        self.assertTrue(jobs[0]['id'])
        self.assertTrue(warnings)
        wrapped=normalize_queue_payload({'schema_version':1,'jobs':jobs},[])
        self.assertEqual(wrapped[0]['filename'],'model.safetensors')

    def test_duplicate_queue_ids_are_repaired_and_duplicate_paths_are_skipped(self):
        base={'repo':'owner/model','revision':'main','connections':8,'status':'paused'}
        warnings=[]
        jobs=normalize_queue_payload([
            dict(base,id='same',filename='a.bin',path='C:\\models\\a.bin'),
            dict(base,id='same',filename='b.bin',path='C:\\models\\b.bin'),
            dict(base,id='third',filename='duplicate.bin',path='C:\\models\\a.bin'),
        ],warnings)
        self.assertEqual(len(jobs),2)
        self.assertEqual(len({job['id'] for job in jobs}),2)
        self.assertGreaterEqual(len(warnings),2)

    def test_corrupt_queue_paths_are_skipped(self):
        base={'repo':'owner/model','filename':'model.bin','status':'paused'}
        warnings=[]
        jobs=normalize_queue_payload([
            dict(base,path='relative\\model.bin'),
            dict(base,path='C:\\models\\bad\x00name.bin'),
            dict(base,path='C:\\models\\..\\outside.bin'),
        ],warnings)
        self.assertFalse(jobs)
        self.assertEqual(len(warnings),3)

    def test_worker_pool_uses_a_fixed_number_of_daemon_threads(self):
        pool=DaemonWorkerPool(2)
        try:
            self.assertEqual(len(pool.threads),2)
            self.assertTrue(all(worker.daemon for worker in pool.threads))
            for _ in range(100):pool.submit(lambda:None)
            self.assertEqual(len(pool.threads),2)
        finally:pool.shutdown(cancel_futures=True)

    def test_early_window_close_stops_engine_before_widgets_exist(self):
        app=object.__new__(App)
        app.closing=False
        events=[]
        app.stop=SimpleNamespace(set=lambda:events.append('stop'))
        app.workers=SimpleNamespace(shutdown=lambda **kwargs:events.append(('workers',kwargs)))
        app.engine=SimpleNamespace(close=lambda:events.append('engine'))
        app.withdraw=lambda:events.append('withdraw')
        app.destroy=lambda:events.append('destroy')
        App.close_app(app)
        App.close_app(app)
        self.assertEqual(events,['stop','withdraw',('workers',{'wait':False,'cancel_futures':True}),'engine','destroy'])

    def test_delete_is_cancelled_when_engine_cannot_confirm_stop(self):
        app=object.__new__(App)
        app.jobs={'job':{'id':'job','gid':'gid','status':'removing','removing':True,'delete_files':True,
                         'remove_previous_status':'active','filename':'model.bin','path':'C:\\models\\model.bin',
                         'connections':8,'elapsed':0}}
        class BrokenEngine:
            def call(self,method,*_):
                if method=='forceRemove':raise RuntimeError('busy')
                if method=='tellStatus':return {'status':'active'}
                raise AssertionError(method)
        app.engine=BrokenEngine();finished=[];notes=[]
        app.finish_remove=lambda *args:finished.append(args)
        app.render=lambda *_:None;app.persist=lambda:None;app.refresh_summary=lambda:None;app.selection_changed=lambda:None
        app.note=SimpleNamespace(set=notes.append)
        def submit(func,success,failure):
            try:success(func())
            except Exception as exc:failure(str(exc))
        app.submit=submit
        App.stop_and_finish_remove(app,'job')
        self.assertFalse(finished)
        self.assertEqual(app.jobs['job']['status'],'active')
        self.assertNotIn('removing',app.jobs['job'])
        self.assertTrue(notes)

    def test_runtime_data_moves_outside_the_program_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)/'program';local=Path(directory)/'local'
            (base/'data').mkdir(parents=True)
            (base/'data'/'queue.json').write_text('[{"repo":"a/b"}]',encoding='utf-8')
            with patch.object(app_module,'BASE',base),patch.dict(os.environ,{'LOCALAPPDATA':str(local)}):
                target=data_directory()
            self.assertEqual(target,local/'HF下载器')
            self.assertTrue((target/'queue.json').is_file())
            self.assertTrue((target/'migration-v1.done').is_file())

    def test_failed_runtime_data_migration_is_retried_later(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)/'program';local=Path(directory)/'local'
            (base/'data').mkdir(parents=True)
            (base/'data'/'queue.json').write_text('[]',encoding='utf-8')
            with patch.object(app_module,'BASE',base),patch.dict(os.environ,{'LOCALAPPDATA':str(local)}),patch.object(app_module.shutil,'copy2',side_effect=OSError('locked')):
                target=data_directory()
            self.assertFalse((target/'migration-v1.done').exists())

    def test_windows_overlong_target_is_rejected(self):
        with patch.object(app_module.os,'name','nt'):
            with self.assertRaises(ValueError):
                validate_target_path('C:\\'+('very-long-folder\\'*20)+'model.safetensors')

    def test_insufficient_disk_space_is_rejected_before_download(self):
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'model.safetensors'
            with patch.object(app_module.shutil,'disk_usage',return_value=SimpleNamespace(free=1024**3)):
                with self.assertRaises(ValueError):
                    ensure_download_capacity(target,2*1024**3)

class SelectionBatchTests(unittest.TestCase):
    def make_app(self):
        app=object.__new__(App)
        app.jobs={}
        app.connections=SimpleNamespace(get=lambda:'8')
        app.tree=SimpleNamespace(insert=lambda *_args,**_kwargs:None)
        app.empty_queue=SimpleNamespace(place_forget=lambda:None)
        app.note=SimpleNamespace(set=lambda value:None)
        app.render=lambda job:None
        app.refresh_summary=lambda:None
        app.persist=lambda:None
        app.selection_changed=lambda:None
        submitted=[]
        app.submit=lambda *args:submitted.append(args)
        return app,submitted

    def test_staged_file_is_visible_without_starting_resolution(self):
        app,submitted=self.make_app()
        with tempfile.TemporaryDirectory() as directory:
            info=parse_hf_url('https://huggingface.co/owner/model/resolve/main/weights.bin')
            ident=App.enqueue(app,info,directory,'','',staged=True,known_size=1024)
            self.assertEqual(app.jobs[ident]['status'],'staged')
            self.assertEqual(app.jobs[ident]['size'],1024)
            self.assertFalse(submitted)

    def test_final_confirmation_starts_all_staged_files(self):
        app,submitted=self.make_app()
        app.jobs={'a':{'status':'staged'},'b':{'status':'staged'}}
        session={'ids':['a','b'],'cancelled':False,'proxy':'','token':''}
        with patch.object(App,'decision_dialog') as ask:
            App.finish_selection_session(app,session)
        ask.assert_not_called()
        self.assertEqual([app.jobs[key]['status'] for key in ('a','b')],['preparing','preparing'])
        self.assertEqual(len(submitted),2)

    def test_closing_partial_selection_asks_and_keeps_jobs_paused(self):
        app,submitted=self.make_app()
        app.jobs={'a':{'status':'staged'}}
        session={'ids':['a'],'cancelled':True,'proxy':'','token':''}
        with patch.object(App,'decision_dialog',return_value='keep') as ask:
            App.finish_selection_session(app,session)
        ask.assert_called_once()
        self.assertEqual(app.jobs['a']['status'],'paused')
        self.assertFalse(submitted)

    def test_closing_partial_selection_can_start_added_jobs(self):
        app,submitted=self.make_app()
        app.jobs={'a':{'status':'staged'}}
        session={'ids':['a'],'cancelled':True,'proxy':'','token':''}
        with patch.object(App,'decision_dialog',return_value='start') as ask:
            App.finish_selection_session(app,session)
        ask.assert_called_once()
        self.assertEqual(app.jobs['a']['status'],'preparing')
        self.assertEqual(len(submitted),1)

    def test_closing_partial_selection_can_remove_added_records(self):
        app,submitted=self.make_app()
        app.jobs={'a':{'status':'staged'}}
        removed=[]
        app.remove_ids=lambda ids,delete_files:removed.append((ids,delete_files))
        session={'ids':['a'],'cancelled':True,'proxy':'','token':''}
        with patch.object(App,'decision_dialog',return_value='remove'):
            App.finish_selection_session(app,session)
        self.assertEqual(removed,[(['a'],False)])
        self.assertFalse(submitted)

class DownloadIntegration(unittest.TestCase):
    def test_parallel_pause_restart_and_checksum(self):
        content=os.urandom(12*1024*1024)
        digest=hashlib.sha256(content).hexdigest()
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                start,end=0,len(content)-1
                match=re.match(r'bytes=(\d+)-(\d*)',self.headers.get('Range',''))
                if match:
                    start=int(match[1]);end=min(int(match[2]) if match[2] else end,end)
                self.send_response(206 if match else 200)
                self.send_header('Content-Length',str(end-start+1))
                self.send_header('Accept-Ranges','bytes')
                if match:self.send_header('Content-Range',f'bytes {start}-{end}/{len(content)}')
                self.end_headers()
                try:
                    for offset in range(start,end+1,32768):
                        self.wfile.write(content[offset:min(offset+32768,end+1)])
                        self.wfile.flush();time.sleep(.006)
                except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        binary=Path(__file__).parent/'vendor'/'aria2c.exe'
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);engine=Engine(binary,base/'state',2)
            def job(name,connections):
                return {'url':f'http://127.0.0.1:{server.server_port}/{name}','path':str(base/name),'connections':connections,'sha256':digest}
            first,second=job('one.bin',8),job('two.bin',16)
            try:
                gid1=engine.add(first);gid2=engine.add(second)
                time.sleep(.4)
                self.assertEqual(len(engine.call('tellActive')),2)
                self.assertEqual(engine.call('getOption',gid1)['max-connection-per-server'],'8')
                self.assertEqual(engine.call('getOption',gid2)['max-connection-per-server'],'16')
                engine.call('forcePause',gid1)
                time.sleep(.2)
                self.assertEqual(engine.call('tellStatus',gid1)['status'],'paused')
                engine.close()
                self.assertIsNotNone(engine.process.poll())
                self.assertTrue((base/'one.bin.hfdownload.aria2').exists())
                engine=Engine(binary,base/'state',2)
                gid1=engine.add(first);gid2=engine.add(second)
                deadline=time.time()+25
                while time.time()<deadline:
                    states=[engine.call('tellStatus',g)['status'] for g in (gid1,gid2)]
                    if all(s=='complete' for s in states):break
                    if 'error' in states:self.fail(str([engine.call('tellStatus',g) for g in (gid1,gid2)]))
                    time.sleep(.2)
                self.assertEqual(states,['complete','complete'])
                for name in ('one.bin','two.bin'):
                    self.assertEqual(hashlib.sha256((base/(name+'.hfdownload')).read_bytes()).hexdigest(),digest)
                # A changed revision must never reuse another revision's partial data.
                modified=dict(first,url=first['url']+'?new-version')
                with self.assertRaises(ValueError):engine.add(modified)
            finally:engine.close()
        server.shutdown();server.server_close()

if __name__=='__main__':unittest.main(verbosity=2)
