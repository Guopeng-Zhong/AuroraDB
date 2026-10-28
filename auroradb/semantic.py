"""Bounded semantic relational operators over a materialized SELECT snapshot.

Inspired by iPDB (arXiv:2601.16432), not a reproduction of its optimizer. The
ordinary SQL engine remains deterministic and independent of model availability.
Only the selected text column crosses the model boundary. Results are probabilistic
annotations, not SQL equivalence evidence. Cache entries are exact text/prompt/model
matches, held in memory with bounded size and TTL, never persisted with credentials.
"""
from __future__ import annotations

from collections import Counter, OrderedDict
import copy
import hashlib
import json
import math
import threading
import time

from .ai import AIError
from .engine import DatabaseError,metric_delta


class SemanticExecutor:
    def __init__(self, service, capacity=512, ttl_seconds=300):
        if type(capacity) is not int or not 1<=capacity<=4096:
            raise ValueError('Invalid semantic cache capacity')
        if not math.isfinite(ttl_seconds) or ttl_seconds<=0:
            raise ValueError('Invalid semantic cache TTL')
        self.service=service
        self.capacity=capacity
        self.ttl_seconds=ttl_seconds
        self.cache=OrderedDict()
        self.lock=threading.Lock()
        self.hits=self.misses=self.evictions=0
        self.epoch=0

    def status(self):
        with self.lock:
            self._expire()
            return {'entries':len(self.cache),'capacity':self.capacity,
                    'ttl_seconds':self.ttl_seconds,'hits':self.hits,'misses':self.misses,
                    'evictions':self.evictions,'operations':['filter','classify','aggregate'],
                    'data_policy':'仅发送本次所选文本字段；NULL 不发送。',
                    'scope':'显式语义算子管线；未扩展 SQL 语法，不支持语义连接。'}

    def clear(self):
        with self.lock:
            self.cache.clear()
            self.epoch+=1
            return {'cleared':True,'entries':0}

    def _expire(self):
        now=time.monotonic()
        for key,(_,created) in list(self.cache.items()):
            if now-created>=self.ttl_seconds: del self.cache[key]

    def _key(self,prompt,text):
        # Include provider identity. A model or endpoint change cannot reuse another
        # provider's decisions; no endpoint/key appears in status or output.
        identity=[self.service.mode,self.service.model,getattr(self.service,'_endpoint',None),
                  getattr(self.service,'_api_key',''),'semantic-v1',prompt,text]
        return hashlib.sha256(json.dumps(identity,ensure_ascii=False).encode('utf-8')).hexdigest()

    @staticmethod
    def _integer(value,name,low,high):
        if type(value) is not int or not low<=value<=high:
            raise DatabaseError(f'{name} 必须是 {low} 到 {high} 的整数。')
        return value

    def run(self,database,sql,column,prompt,operation='filter',batch_size=8,max_rows=64,
            max_calls=8,budget_seconds=30,use_cache=True):
        start=time.perf_counter()
        batch_size=self._integer(batch_size,'批大小',1,16)
        max_rows=self._integer(max_rows,'记录预算',1,256)
        max_calls=self._integer(max_calls,'调用预算',1,32)
        if (type(budget_seconds) not in (int,float) or not math.isfinite(budget_seconds)
                or not 0.1<=budget_seconds<=90):
            raise DatabaseError('时间预算必须是 0.1 到 90 秒。')
        if type(use_cache) is not bool: raise DatabaseError('缓存开关必须是布尔值。')
        if operation not in {'filter','classify','aggregate'}:
            raise DatabaseError('语义操作只支持 filter、classify、aggregate。')
        if not isinstance(column,str) or not column.strip(): raise DatabaseError('请选择一个文本输出字段。')
        if not isinstance(sql,str) or len(sql)>20000:
            raise DatabaseError('语义实验 SQL 最多 20000 字符。')
        if not isinstance(prompt,str) or not prompt.strip() or len(prompt)>8000:
            raise DatabaseError('请输入最多 8000 字符的语义判断条件。')
        if not self.service.status()['available']:
            raise AIError('语义算子需要启用学校模型或本地关键词演示。')
        # Bind/read only. Never call execute(): a diagnostic error must not roll back
        # a user's transaction. Materialize once, then release both DB/server locks
        # before any network operation. Later writes do not alter this snapshot.
        with database.lock:
            if database.active: raise DatabaseError('请先提交或回滚事务，再运行语义实验。')
            statement=database._diagnostic_select(sql)
            database._select(statement,explain_only=True)
            saved=database.counters
            saved_budget=database._research_budget
            before=database.pager.stats()
            database.counters={'rows_scanned':0,'index_lookups':0,'join_comparisons':0}
            database._research_budget={'deadline':start+min(6.,budget_seconds),
                                       'max_scanned':100000,'max_comparisons':200000,
                                       'max_rows':20000}
            try:
                result=database._select(statement)
                rows=copy.deepcopy(result['rows'])
                columns=list(result['columns'])
                query_counters=dict(database.counters)
            finally:
                database.counters=saved
                database._research_budget=saved_budget
            after=database.pager.stats()
            query_stats=metric_delta(before,after)
            query_stats.update(query_counters)
        sql_ms=(time.perf_counter()-start)*1000
        if columns.count(column)!=1: raise DatabaseError('字段必须恰好匹配一个 SELECT 输出列；重复别名请先修改。')
        position=columns.index(column)
        if len(rows)>max_rows:
            raise DatabaseError(f'查询返回 {len(rows)} 行，超过本次 {max_rows} 行预算；请先用 WHERE 或 LIMIT 缩小查询。')
        if any(row[position] is not None and (not isinstance(row[position],str) or len(row[position])>2048)
               for row in rows):
            raise DatabaseError('语义字段必须是 TEXT 或 NULL，每条文本最多 2048 字符；不会截断文本。')
        if operation=='classify' and any(c in columns for c in ('_semantic_match','_semantic_label')):
            raise DatabaseError('输出字段与语义标签冲突，请改用其他别名。')
        decisions={}
        unique=OrderedDict()
        for row in rows:
            text=row[position]
            if text is not None: unique.setdefault(self._key(prompt,text),text)
        pending=[]
        hits=0
        with self.lock:
            epoch=self.epoch
            self._expire()
            for key,text in unique.items():
                if use_cache and key in self.cache:
                    decisions[key]=copy.deepcopy(self.cache[key][0])
                    self.cache.move_to_end(key)
                    hits+=1
                else: pending.append((key,text))
            self.hits+=hits
            self.misses+=len(pending)
        needed=(len(pending)+batch_size-1)//batch_size
        if needed>max_calls:
            raise DatabaseError(f'需要 {needed} 个批次，超过 {max_calls} 次调用预算；请增大批大小或减少查询行数。')
        model_calls=batches=0
        inference_start=time.perf_counter()
        deadline=start+budget_seconds
        if inference_start>=deadline:
            raise AIError('语义实验时间预算已耗尽；未返回部分结果。')
        for offset in range(0,len(pending),batch_size):
            remaining=deadline-time.perf_counter()
            if remaining<=0: raise AIError('语义实验时间预算已耗尽；未返回部分结果。')
            selected=pending[offset:offset+batch_size]
            answers=self.service.semantic_batch(prompt,[text for _,text in selected],timeout=remaining)
            batches+=1
            model_calls+=int(self.service.mode=='remote')
            if time.perf_counter()>deadline:
                raise AIError('语义实验时间预算已耗尽；未返回部分结果。')
            # The service validates IDs. Cache only complete, validated batches.
            for (key,_),answer in zip(selected,answers):
                decisions[key]={'match':answer['match'],'label':answer['label']}
            with self.lock:
                if use_cache and self.epoch==epoch:
                    for key,_ in selected:
                        self.cache[key]=(copy.deepcopy(decisions[key]),time.monotonic())
                        self.cache.move_to_end(key)
                        while len(self.cache)>self.capacity:
                            self.cache.popitem(last=False)
                            self.evictions+=1
        output=[]
        labels=Counter()
        for row in rows:
            text=row[position]
            answer={'match':False,'label':'NULL'} if text is None else decisions[self._key(prompt,text)]
            if operation=='filter':
                if answer['match']: output.append(row)
            elif operation=='classify': output.append(row+[answer['match'],answer['label']])
            else: labels[answer['label']]+=1
        if operation=='classify': columns+=['_semantic_match','_semantic_label']
        if operation=='aggregate':
            columns=['semantic_label','count']
            output=[[label,count] for label,count in sorted(labels.items())]
        status=self.service.status()
        operator={'filter':'SemanticFilter','classify':'SemanticProject','aggregate':'SemanticAggregate'}[operation]
        return {'columns':columns,'rows':output,'row_count':len(output),'input_rows':len(rows),
                'operation':operation,'column':column,'mode':status['mode'],'provider':status['provider'],
                'plan':{'operator':operator,'detail':f'{column}; batch={batch_size}; budget={max_rows}',
                        'estimated_rows':len(output),'children':[result['plan']] if result.get('plan') else []},
                'stats':{'model_calls':model_calls,'batches':batches,'cache_hits':hits,
                         'cache_misses':len(pending),'unique_texts':len(unique),
                         'deduplicated_rows':sum(r[position] is not None for r in rows)-len(unique),
                         'null_rows':sum(r[position] is None for r in rows),'sql_elapsed_ms':round(sql_ms,3),
                         'inference_elapsed_ms':round((time.perf_counter()-inference_start)*1000,3),
                         'elapsed_ms':round((time.perf_counter()-start)*1000,3),'budget_rows':max_rows,
                         'budget_calls':max_calls,'budget_seconds':budget_seconds,
                         'sql_budget':{'seconds':min(6.,budget_seconds),'scanned_rows':100000,
                                       'join_comparisons':200000,'intermediate_rows':20000},'query':query_stats},
                'explanation':('本地关键词规则演示，没有调用大模型。' if status['mode']=='mock' else
                               '学校兼容模型根据所选文本字段给出判断；标签不构成 SQL 等价证明。'),
                'snapshot':'提交态 SELECT 的一次物化快照；推理期间的后续写入不改变此结果。',
                'cache_policy':'按完整文本、条件和模型配置精确匹配；内存缓存 5 分钟；NULL 不调用模型。',
                'data_policy':'只发送所选字段的文本和判断条件；不发送其他列、原 SQL 或表结构。'}
