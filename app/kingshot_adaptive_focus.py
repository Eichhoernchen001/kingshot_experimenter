"""Budgeted, reversible focus for four-joiner sampling (checkpoint version 2).
Selection intervals are approximate HC3 model intervals, not exact sequential CIs.
Alpha spending limits repeated nominal tests; held-out validation checks predictions.
"""
import copy, itertools, math
import numpy as np
from scipy.stats import norm
from kingshot_adaptive import LegacyAdaptiveExperiment, design_order

class FocusedAdaptiveExperiment(LegacyAdaptiveExperiment):
    ENGINE_VERSION=3
    STATE_VERSION=2

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        for key,value in dict(decisions=[],rank_looks=0,extra_copy_heroes=[],model_heroes=[],
                              refinement_used=0,stable_checks=0,soft_lock=None).items():
            self.state.setdefault(key,value)
        self.save()

    def training(self):
        return [o for o in self.state['observations'] if o['phase']!='validate']

    def _interests(self):
        requested={name.strip().casefold() for name in self.o.get('heroes_of_interest',[])}
        return [i for i,name in enumerate(self.names) if name.casefold() in requested]

    def _assess(self):
        obs=self.training();ids=[o['index'] for o in obs]
        # Extra-copy columns prevent a good second/third copy being attributed to
        # the first-copy effect. Initial broad screening stays in every fit.
        cols=[self.S]
        for h in range(self.n):
            for k in range(2,int(self.C[ids,h].max())+1):cols.append((self.C[:,h]>=k)[:,None].astype(float))
        A=np.column_stack(cols);X=A[ids];y=np.array([o['row']['winrate']/100 for o in obs])
        inv=np.linalg.pinv(X);b=inv@y;V=inv@inv.T;res=y-X@b;leverage=np.sum(X*inv.T,axis=1)
        noise=np.maximum(.25/self.trials,(res/np.maximum(1-leverage,.05))**2)
        cov=(inv*noise)@inv.T;est=self.H@b[1:self.n]
        self.state['rank_looks']+=1;look=self.state['rank_looks']
        # Sum 1/(t(t+1)) = 1. Half the nominal error budget is for hero decisions.
        critical=norm.ppf(1-.025/(self.n*(self.n-1)*look*(look+1)))
        beats=np.zeros((self.n,self.n),bool)
        for i,j in itertools.combinations(range(self.n),2):
            L=np.zeros(A.shape[1]);L[1:self.n]=self.H[i]-self.H[j]
            if np.linalg.norm(L-inv@X@L)>1e-7:continue
            width=critical*math.sqrt(max(float(L@cov@L),0))
            delta=est[i]-est[j];beats[i,j]=delta>width;beats[j,i]=-delta>width
        exact_ranking=np.argsort(-est)
        # Do not churn the shortlist when peers differ by less than the user's
        # practical tolerance. Previous order is a tie-break, not an effect prior.
        previous=self.state.get('ranking',exact_ranking.tolist());remaining=set(range(self.n));ranked=[]
        tolerance=float(self.o['practical_tolerance_pp'])/100
        while remaining:
            best_value=max(est[i] for i in remaining)
            tied={i for i in remaining if best_value-est[i]<=tolerance}
            chosen=next(i for i in previous if i in tied)
            ranked.append(chosen);remaining.remove(chosen)
        ranking=np.array(ranked)
        unresolved=np.flatnonzero(beats.sum(0)<self.target) if self.o['mode']=='fast' else np.flatnonzero(~beats.any(0))
        keep=np.union1d(unresolved,ranking[:self.target]).astype(int).tolist()
        best=int(exact_ranking[0]);standout=best if np.all(beats[best,np.arange(self.n)!=best]) else None
        return est,keep,standout,ranking

    def eligible(self):
        keep=self.state['keep'];outside=np.setdiff1d(np.arange(self.n),keep)
        ok=np.all(self.C[:,outside]==0,axis=1)
        original={h:int(k) for k,values in self.cfg['joiner'][self.side]['pools'].items() for h in values}
        for i,name in enumerate(self.names):
            cap=max(int(self.o['duplicate_limit']),original.get(name,1),int(self.minimum[i]))
            if i in self.state.get('extra_copy_heroes',[]):cap=max(cap,int(self.o['standout_max_copies']))
            ok &= self.C[:,i]+self.minimum[i]<=cap
        return np.flatnonzero(ok)

    def features(self):
        C=self.C;cols=[np.ones(len(C)),*(C@self.H).T]
        terms=[('baseline',),*[('contrast',i) for i in range(self.n-1)]];pen=[1e-8]+[.0625]*(self.n-1)
        # Keep the basis stable when a hero is provisionally removed or restored.
        # Unmeasured copies stay regularized and are omitted from contribution CIs.
        for h in range(self.n):
            for k in range(2,int(C[:,h].max())+1):
                cols.append((C[:,h]>=k).astype(float));terms.append(('copy',h,k));pen.append(.25)
        for size,layer in [(2,'pair'),(3,'triple')]:
            if not self.cfg.get('analysis',{}).get(layer+'_synergy',True):continue
            for heroes in itertools.combinations(self.state.get('model_heroes',self.state['keep']),size):
                cols.append(np.all(C[:,heroes]>0,axis=1).astype(float));terms.append((layer,*heroes));pen.append(2.7777778)
        return np.array(cols).T,terms,np.array(pen)

    def _update(self):
        est,keep,standout,ranking=self._assess()
        old=set(self.state['keep']);self.state['keep']=keep;self.state['standout']=standout
        self.state['soft_lock']=standout
        self.state['model_heroes']=sorted(set(self.state['model_heroes'])|set(keep))
        # Testing higher copies does not require declaring a hero a standout.
        self.state['extra_copy_heroes']=sorted(set(self.state['extra_copy_heroes'])|set(map(int,ranking[:2])))
        self.state['ranking']=ranking.astype(int).tolist()
        self.state['decisions'].append(dict(training_batches=len(self.training()),
            estimates_pp=(est*100).tolist(),retained=[self.names[i] for i in keep],
            restored=[self.names[i] for i in sorted(set(keep)-old)] if old else [],
            standout=self.names[standout] if standout is not None else None,
            soft_lock=self.names[standout] if standout is not None else None))
        print('Adaptive reassessment: '+', '.join(self.names[i] for i in keep)+
              ('; provisional fixed slot: '+self.names[standout] if standout is not None else '; no fixed slot'),flush=True)
        return est

    def _copy_probes(self,F,beta,eligible):
        counts=np.bincount([o['index'] for o in self.training()],minlength=len(self.C))
        result=[];pred=F@beta
        for h in self.state['ranking'][:2]:
            maximum=min(int(self.o['standout_max_copies']),int((self.C[eligible,h]+self.minimum[h]).max()))
            for k in range(2,maximum+1):
                if k==2 and any(self.C[o['index'],h]+self.minimum[h]==2 for o in self.training()):continue
                candidates=eligible[self.C[eligible,h]+self.minimum[h]==k]
                # Third-copy teams have only one remaining partner: testing all
                # finalist partners is cheap and avoids missing 3x leader + X.
                candidates=candidates[np.argsort(-pred[candidates])]
                if k!=3 or h!=self.state['ranking'][0]:candidates=candidates[:1]
                for j in candidates:
                    if counts[j]==0 and int(j) not in result:result.append(int(j))
        return result

    def _queue_refinement(self,F,beta,cov,V,eligible,number):
        obs=self.training();counts=np.bincount([o['index'] for o in obs],minlength=len(F))
        pred=F@beta;best=int(eligible[np.argmax(pred[eligible])]);probes=self._copy_probes(F,beta,eligible)
        selected=[];purposes=[];V=V.copy();noise=max(.25/self.trials,float(np.max(np.diag(cov))/max(np.max(np.diag(V)),1e-12)))
        def add(j,purpose):
            selected.append(int(j));purposes.append(purpose);counts[j]+=1
            v=V@F[j];V[:]-=np.outer(v,v)/(1+F[j]@v)
        # At least one in five batches challenges the shortlist or the fixed slot.
        audits=max(1,math.ceil(number*.2));lock=self.state.get('soft_lock')
        outside=np.setdiff1d(np.arange(self.n),self.state['keep'])
        broad=np.union1d(self.screen,eligible)
        pool=broad
        if len(outside):pool=pool[np.any(self.C[pool][:,outside]>0,axis=1)]
        elif lock is not None:pool=pool[self.C[pool,lock]==0]
        for audit in range(min(audits,len(pool))):
            audit_pool=broad[self.C[broad,lock]==0] if lock is not None and audit==0 else pool
            if not len(audit_pool):audit_pool=pool
            # Retest underrepresented challenge teams; interest names get only
            # a scheduling preference, never a signed effect or threshold change.
            candidates=audit_pool[counts[audit_pool]==counts[audit_pool].min()]
            scores=pred[candidates]+2*np.sqrt(np.maximum(np.sum((F[candidates]@cov)*F[candidates],axis=1),0))
            interests=self._interests()
            if interests:scores+=.001*np.sum(self.C[candidates][:,interests]>0,axis=1)
            add(int(candidates[np.argmax(scores)]),'challenge')
        for j in probes:
            if len(selected)>=number:break
            add(j,'copy probe')
        if len(selected)<number:add(best,'incumbent')
        while len(selected)<number:
            candidates=eligible
            if lock is not None:
                focused=eligible[self.C[eligible,lock]>0]
                if len(focused):candidates=focused
            D=F[candidates]-F[best];variance=np.maximum(np.sum((D@V)*D,axis=1),0)
            gap=np.maximum(pred[best]-pred[candidates],0)
            tolerance=max(float(self.o['practical_tolerance_pp'])/100,.01)
            score=variance/(gap+tolerance)**2
            # Repeats are allowed as soon as they best resolve a comparison.
            maxima=np.flatnonzero(np.isclose(score,score.max(),rtol=1e-8,atol=1e-10))
            add(int(candidates[int(self.rng.choice(maxima))]),'decision')
        self.state['queue_purposes']=purposes;self.set_queue(selected)

    def _refine(self,reassess=True):
        if reassess:self._update()
        eligible=self.eligible()
        if not len(eligible):raise ValueError('No finalist lineup satisfies the configured copy limits.')
        F,terms,beta,cov,V=self.model();pred=F@beta;ranked=eligible[np.argsort(-pred[eligible])]
        used=sum(o['phase']=='refine' for o in self.state['observations'])
        self.state['refinement_used']=used
        maximum=self.state['refinement_cap'];minimum=min(maximum,int(self.o['refinement_min_batches_per_hero'])*self.state['initial_finalist_count'])
        best=int(ranked[0]);near=ranked[:min(10,len(ranked))];previous=self.state.get('previous_predictions')
        stable=previous is not None and max(abs(pred[near]-np.array(previous)[near]))<=float(self.o['practical_tolerance_pp'])/100
        previous_best=self.state.get('previous_best')
        same_quality=previous_best in set(eligible.tolist()) and pred[best]-pred[previous_best]<=float(self.o['practical_tolerance_pp'])/100
        if stable and same_quality:self.state['stable_checks']+=1
        else:self.state['stable_checks']=0
        self.state['previous_predictions']=pred.tolist();self.state['previous_best']=best
        D=F[eligible]-F[best];se=np.sqrt(np.maximum(np.sum((D@cov)*D,axis=1),0))
        look=max(1,self.state['rank_looks']);critical=norm.ppf(1-.025/(max(1,len(eligible))*look*(look+1)))
        uncertain_upper=float(np.max(pred[eligible]-pred[best]+critical*se))
        pending=self._copy_probes(F,beta,eligible)
        resolved=uncertain_upper<=float(self.o['practical_tolerance_pp'])/100
        stable_stop=self.o['mode']=='faster' and self.state['stable_checks']>=2
        if used>=maximum:reason='refinement budget reached'
        elif used>=minimum and not pending and resolved:reason='remaining model differences within tolerance'
        elif used>=minimum and not pending and stable_stop:reason='stable recommendation (Faster heuristic; check validation)'
        else:reason=None
        if reason:
            self.state['stop_reason']=reason;self._start_validation(F,terms,beta,cov,eligible);return
        self._queue_refinement(F,beta,cov,V,eligible,min(int(self.o['decision_batch_size']),maximum-used))

    def _start_validation(self,F,terms,beta,cov,eligible):
        pred=np.clip(F@beta,0,1);ranked=eligible[np.argsort(-pred[eligible])]
        pool=np.union1d(self.screen,eligible)
        maximum=min(int(self.o['validation_lineups']),len(pool))
        # Two teams per finalist is the working validation set; the old setting
        # remains a ceiling. Larger settings no longer force a large random set.
        total=min(maximum,max(4,2*len(self.state['keep'])))
        selected=list(map(int,ranked[:min(max(1,total//3),len(ranked))]))
        besthero=self.state['ranking'][0]
        for k in range(2,int(self.o['standout_max_copies'])+1):
            candidates=ranked[self.C[ranked,besthero]+self.minimum[besthero]==k]
            if len(candidates) and int(candidates[0]) not in selected and len(selected)<total:selected.append(int(candidates[0]))
        # Include a no-leader challenger even when a slot was provisionally fixed.
        challengers=pool[self.C[pool,besthero]==0]
        if len(challengers) and len(selected)<total:
            candidate=int(challengers[np.argmax(pred[challengers])])
            if candidate not in selected:selected.append(candidate)
        remaining=np.setdiff1d(pool,selected)
        selected+=self.rng.choice(remaining,total-len(selected),replace=False).astype(int).tolist()
        self.state['frozen_model']=dict(prediction=pred.tolist(),beta=beta.tolist(),covariance=cov.tolist(),terms=[list(t) for t in terms],recommended_index=int(ranked[0]))
        self.state['validation_teams']=selected;self.state['stage']='validate';self.state['validation_pass']=1
        repeats=min(2,int(self.o['validation_batches']))
        self.set_queue(np.repeat(selected,repeats))
        print(f'Frozen model: {self.state["stop_reason"]}. Validation starts with {total} teams x {repeats} batches.',flush=True)

    def _validation(self):
        groups={}
        for o in self.state['observations']:
            if o['phase']=='validate':groups.setdefault(o['index'],[]).append(o['row']['winrate']/100)
        maximum=int(self.o['validation_batches']);pred=np.array(self.state['frozen_model']['prediction'])
        eligible=set(self.eligible().tolist());candidates=[i for i in groups if i in eligible]
        best=max(candidates,key=lambda i:np.mean(groups[i]));target=np.mean(groups[best]);tolerance=float(self.o['practical_tolerance_pp'])/100
        selected=[]
        for i,values in groups.items():
            if len(values)>=maximum:continue
            mean=float(np.mean(values));se=math.sqrt(.25/(len(values)*self.trials)+.25/(len(groups[best])*self.trials))
            # Repeat plausible winners and prediction failures, not every team.
            if (i in eligible and mean+2*se>=target-tolerance) or abs(mean-pred[i])>max(tolerance,2*math.sqrt(.25/(len(values)*self.trials))):selected.append(i)
        if selected:
            self.state['validation_pass']+=1;self.set_queue(selected);return
        self.state['validated_best_index']=int(best);self.state['stage']='done';self.set_queue([])
        print('Adaptive sampling and independent validation complete.',flush=True)

    def advance(self):
        if self.state['stage']=='screen':
            if self.state['round']:
                prior=self.state['history'][-1]['retained'] if self.state['history'] else None
                estimates=self._update()
                retained=[self.names[i] for i in self.state['keep']]
                plateau=prior==retained and self.state['round']>=2
                self.state['history'].append(dict(round=self.state['round'],estimates_pp=(estimates*100).tolist(),retained=[self.names[i] for i in self.state['keep']],screen_batches=len(self.training())))
                if len(self.state['keep'])<=self.target or plateau or self.state['round']>=int(self.o['screen_max_rounds']):
                    self.state['stage']='refine';self.state['initial_finalist_count']=len(self.state['keep'])
                    self.state['refinement_cap']=int(self.o['refinement_batches_per_hero'])*len(self.state['keep'])
                    if plateau:print('Screening shortlist unchanged; retaining unresolved heroes and moving to refinement.',flush=True)
                    self._refine(False);return
            self.state['round']+=1
            ids=[o['index'] for o in self.training()]
            V=np.linalg.inv(np.eye(self.n)*1e-6+self.S[ids].T@self.S[ids]) if ids else None
            order=design_order(self.S[self.screen],int(self.o['screen_batches_per_hero'])*self.n,self.rng,V)
            indices=self.screen[order]
            interests=self._interests()
            if interests:
                # Reorder a balanced design, without removing any hero coverage.
                score=np.sum(self.C[indices][:,interests]>0,axis=1)
                indices=indices[np.argsort(-score,kind='stable')]
            self.state['queue_purposes']=['screen']*len(indices);self.set_queue(indices);return
        if self.state['stage']=='refine':self._refine();return
        if self.state['stage']=='validate':self._validation()

    def record(self,row):
        phase=self.state['stage'];cursor=self.state['cursor']
        observation=dict(index=self.state['queue'][cursor],phase=phase,row=copy.deepcopy(row))
        if phase=='refine':observation['purpose']=self.state['queue_purposes'][cursor]
        self.state['observations'].append(observation);self.state['cursor']+=1
        self.save()
