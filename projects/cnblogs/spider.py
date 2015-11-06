# -*- coding: utf-8 -*-
#!/usr/bin/env python
import re
from easyspider.spider import EasySpider
from easyspider.cron import every, config

class Spider(EasySpider):
    
    @every(crontab=["*/30 * * * *"])
    def on_start(self):
        self.fetch("http://www.cnblogs.com/", callback=self.on_index_page)

    @config(age=30)
    def on_index_page(self, response):
        for each in response.doc('a[href^="http://www.cnblogs.com"]').items():
            if re.match("http://www.cnblogs.com/\s+/$", each.attr.href):
                self.fetch(each.attr.href, callback=self.on_index_page)
            elif re.match("http://www.cnblogs.com/\s+/archive/\d+/\d+\.html$", each.attr.href):
                self.fetch(each.attr.href, callback=self.on_index_page)
            elif re.match("http://www.cnblogs.com/\w+/p/\d+\.html$", each.attr.href):
                self.fetch(each.attr.href, callback=self.on_detail_page)
            
    @config(priority=9)
    def on_detail_page(self, response):
        return {"title": response.doc('h1').text(), "url": response.url}

