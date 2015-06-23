# -*- coding: utf-8 -*-
#!/usr/bin/env python
import gevent
from gevent import monkey, queue

# monkey.patch_all()
import os
import json
import time
import logging
import traceback

from .utils import merge_cookie, import_object
from .mq import build_queue
from .fetcher import Fetcher
from .config import import_config, Config
from .db import Session, ScopedSession, SpiderProject, SpiderTask, SpiderScheduler,SpiderResult




class EasyCrawler:
    def __init__(self, timeout=5, workers_count=5, min_capacity=10, pipeline_size=100, loop_once=False):
        self.load_projects()
        self.load_spiders()
        self.timeout = timeout
        self.loop_once = loop_once
        self.qin = build_queue("redis")
        # self.qout = build_queue("redis") 
        self.jobs = [gevent.spawn(self.do_scheduler)]
        self.jobs += [gevent.spawn(self.do_task)]
        for project in self.projects:
            self.jobs += [gevent.spawn(self.do_worker, project.name, self.spiders.get(project.name))]
        # self.jobs += [gevent.spawn(self.do_worker) for i in range(workers_count)]
        # self.jobs += [gevent.spawn(self.do_pipeline)]
        self.job_count = len(self.jobs)
        # self.lock = threading.Lock()
        self.fetcher = Fetcher()
        self.db = ScopedSession()

    def load_projects(self):
        projects =  SpiderProject.load_projects() #query.filter_by(status=1).all()
        self.projects = []
        now = time.time()
        for project in projects:
            prj = Config()
            prj.name = project.name
            prj.load_time = now
            self.projects.append(prj)

    def load_spiders(self):
        self.spiders = {}
        self.taskqs = {}
        for project in self.projects:
            try:
                Spider = import_object("projects.%s.spider.Spider"% (project.name))
                config = import_config("projects/%s/project.yaml" % (project.name))
                spider = Spider()
                spider.config = config
                spider._on_start()
                self.spiders[project.name] = spider
                self.taskqs[project.name] = build_queue("redis", qname="q_"+project.name)
            except Exception as e:
                logging.error("load spider!\n%s" % traceback.format_exc())
                raise e
        
        print("task qs:", self.taskqs)

    def start(self):
        gevent.joinall(self.jobs)

    def do_scheduler(self):
        now = time.time()
        schedulers = SpiderScheduler.query.filter(SpiderScheduler.next_time<=now).all()
        for s in schedulers:
            if s.process is None or s.process == "":
                process = {}
            else:
                process = json.loads(s.process)
            task = {}
            task['type'] = 'scheduler'
            task['id'] = s.id
            task['project'] = s.project
            task['task_id'] = s.task_id
            task['url'] = s.url
            # if 'crontab' in process:
            #     crons = process.get('crontab')
            #     cronjob = CronJob(crons)
            #     task['next_time'] = cronjob.next(s.last_time)
            # if 'callback' in process:
            #     task['callback'] = process.get('callback')
            task['process'] = process
            inq = self.taskqs.get(s.project)
            inq.put(task)
        gevent.sleep(2)

    def do_task(self):
        try:
           
            while True:
                now = time.time()
                for project in self.projects:
                    if project.load_time > now:
                        continue
                    taskq = self.taskqs.get(project.name)
                    if taskq.qsize() <= 0:
                        new_tasks = self._load_tasks(project.name)
                        tasks_size = len(new_tasks)
                        if tasks_size <= 0:
                            logging.info('project [%s] load no task' % project.name)
                            #project.load_time = now + 10*1000 
                            continue
                        else:
                            logging.info('project [%s] load %d tasks' % (project.name, tasks_size))
                        for task in new_tasks:
                            # print("put in queue", json.dumps(task))
                            taskq.put(task)

                else:
                    gevent.sleep(2)
        except Exception as e:
            logging.error("Scheduler Error!\n%s" % traceback.format_exc())
        

    def do_worker(self, project_name, spider):

        try:
            # task = self.qin.get()
            taskq = self.taskqs.get(project_name)
            
            while True:

                try:
                    task = taskq.get()
                    if task == StopIteration:
                        break
                    if task is None:
                        gevent.sleep(2)
                        logging.info("worker [%s] get no task, sleep 2 seconds" % (project_name))
                        continue

                    print("do worker get a task", task) 
                    # print("project %s task: " % project, task)
                    self.do_fetch(project_name, spider, task)
                except:
                    logging.error("Worker error!\n%s" % traceback.format_exc())

                
                
        finally:
            
            logging.debug("Worker done, ==========================  job count: %s" % self.job_count)

    
    def do_fetch(self, project, spider, task):
        print("do fetch task: ", task)
        if project != task['project']:
            pass
        headers = spider.config.headers
        url = task.get('url')
        task_id = task.get('task_id')
        if url.startswith('data://'):
            if 'process' in task:
                callback = task.get('process').get('callback')
                callback_func = getattr(spider, callback)
                if callback_func:
                    callback_func()
        else:
            response = self.fetcher.fetch(spider, task, headers)
            if 'set-cookie' in response:
                new_cookie = response['set-cookie']
                old_cookie = headers.get('Cookie', None)
                headers['Cookie'] = merge_cookie(new_cookie, old_cookie)

            if 'result' in response:
                res = response['result']
                sr = SpiderResult.query.filter_by(task_id=task_id).first()
                if sr is None:
                    sr = SpiderResult()
                sr.project = task.get('project')
                sr.task_id = task_id
                sr.url = url
                sr.content = json.dumps(res)
                self.db.add(sr)
            st = SpiderTask.query.filter_by(task_id=task_id).first()
            st.status = response['code']
            st.last_time = time.time()
            self.db.add(st)
            self.db.commit()
            # if 'process' in task:
            #     cb_name = task.get('process').get('callback')
            #     callback = getattr(spider, cb_name)
            #     if callback:
            #         callback(response)


    def _load_tasks(self, project):
        tasks = SpiderTask.query.filter(SpiderTask.project==project, SpiderTask.status==0).order_by('priority').limit(30).all()
        
        new_tasks = []
        for task in tasks:
            task.status = 1
            self.db.add(task)
            new_task={}
            new_task['id'] = task.id
            new_task['task_id'] = task.task_id
            new_task['project'] = task.project
            new_task['url'] = task.url
            if task.process is None or task.process == "":
                new_task['process'] = {}
            else:
                new_task['process'] = json.loads(task.process)
            new_tasks.append(new_task)
        self.db.commit()

        return new_tasks


    # def do_pipeline(self):
    #     pipeline_size = 0
    #     while self.job_count > 1 or not self.qout.empty():
    #         sleep(self.timeout)
    #         logging.debug("pipeline sleep, job count: %d" % self.job_count)
    #         try:
    #             results = []
    #             try:
    #                 i=0
    #                 while i<2:
    #                     i+=1
    #                     pipeline_size += 1
    #                     results.append(self.qout.get_nowait())
                        
    #                 if len(results) > 0:
    #                     self.spider.pipeline(results)
    #             except queue.Empty:
    #                 if len(results) > 0:
    #                     self.spider.pipeline(results)
    #         except:
    #             logging.error("Pipeline error!\n%s" % traceback.format_exc()) 
        

