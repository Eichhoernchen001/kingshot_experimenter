"""Adaptive four-joiner experiments: deterministic designs and resumable batches.
Fast protects the shortlist boundary; Faster stops at a small unresolved-best set.
The complete experiment path is unchanged. No source-model or hero-name priors.
"""
from __future__ import annotations
import copy,csv,hashlib,itertools,json,math
from pathlib import Path
import numpy as np
from scipy.linalg import helmert
from scipy.stats import norm

DEFAULTS=dict(mode='complete',target_heroes=6,screen_batches_per_hero=5,screen_max_rounds=5,
              refinement_batches_per_hero=20,validation_lineups=30,validation_batches=3,
              duplicate_limit=2,standout_max_copies=4,seed=20260916)
LABELS={'complete':'Complete — all combinations','fast':'Fast — conservative shortlist','faster':'Faster — early shortlist'}

def options(design):return {**DEFAULTS,**design.get('sampling',{})}
def validate_options(design):
    o=options(design);errors=[]
    if o['mode'] not in LABELS:return ['Unknown joiner sampling mode.']
    limits={'target_heroes':(2,20),'screen_batches_per_hero':(1,100),'screen_max_rounds':(1,100),
            'refinement_batches_per_hero':(1,200),'validation_lineups':(2,200),'validation_batches':(1,100),
            'duplicate_limit':(1,4),'standout_max_copies':(1,4),'seed':(0,2147483647)}
    for key,(lo,hi) in limits.items():
        try:
            v=int(o[key])
            if v!=float(o[key]) or not lo<=v<=hi:raise ValueError
        except (ValueError,TypeError):errors.append(f'{key} must be an integer from {lo} to {hi}.')
    if o['mode']!='complete':
        pools=[s for s in ('attacker','defender') if any(design[s].get('pools',{}).values())]
        if len(pools)!=1:errors.append('Accelerated mode needs a joiner pool on exactly one side.')
        elif len(set(h for values in design[pools[0]]['pools'].values() for h in values))>20:errors.append('Accelerated mode currently supports at most 20 pooled heroes.')
    return errors

def design_order(X,number,rng,V=None):
    V=np.eye(X.shape[1])*1e6 if V is None else V.copy()
    used=np.zeros(len(X),int);result=[]
    for _ in range(number):
        XV=X@V;v=np.maximum(np.sum(XV*X,axis=1),0)
        score=np.where(used==used.min(),v,-np.inf)
        candidates=np.flatnonzero(np.isclose(score,score.max(),rtol=1e-8,atol=1e-9))
        j=int(rng.choice(candidates));result.append(j);used[j]+=1
        vec=XV[j];V-=np.outer(vec,vec)/(1+v[j])
    return result

def atomic_json(path,value):
    path=Path(path);temp=path.with_name(path.name+'.tmp')
    temp.write_text(json.dumps(value,indent=2,ensure_ascii=False,allow_nan=False),encoding='utf8');temp.replace(path)

class AdaptiveExperiment:
    def __init__(self,conditions,cfg,folder,side,trials,resume=True):
        self.conditions=[tuple(c) for c in conditions];self.cfg=copy.deepcopy(cfg);self.o=options(cfg['joiner'])
        self.folder=Path(folder);self.path=self.folder/'adaptive_state.json';self.side=side;self.trials=int(trials)
        self.offset=0 if side=='attacker' else 4
        names=sorted({h for c in conditions for h in c[self.offset:self.offset+4] if h})
        raw=np.array([[c[self.offset:self.offset+4].count(h) for h in names] for c in conditions],dtype=int)
        if not np.all(raw.sum(1)==4):raise ValueError('Accelerated mode needs four joiners on the varied side. Fill empty slots from a pool or use Complete.')
        fixed=raw.min(0);active=np.ptp(raw,axis=0)>0
        self.fixed={h:int(v) for h,v in zip(names,fixed) if v};self.names=[h for h,a in zip(names,active) if a]
        self.C=raw[:,active]-fixed[active];self.minimum=fixed[active];self.n=len(self.names)
        if self.n<2:raise ValueError('There are fewer than two independently varying heroes. Use Complete for this setup.')
        self.H=helmert(self.n).T;self.S=np.column_stack([np.ones(len(raw)),self.C@self.H])
        self.screen=np.flatnonzero(self.C.max(1)<=1)
        if len(self.screen)==0 or np.linalg.matrix_rank(self.S[self.screen])<self.n:
            raise ValueError('The restricted unique-hero design cannot distinguish all hero effects. Relax the fixed slots or use Complete.')
        self.target=min(int(self.o['target_heroes']),self.n)
        profiles=copy.deepcopy(cfg['profiles'])
        for profile in profiles.values():
            for key in ('name','player_file','input_values_embedded'):profile.pop(key,None)
        signature={'design':cfg['joiner'],'profiles':profiles,'player_setup':cfg.get('player_setup',cfg['battle_setup']),
                   'trials':self.trials,'side':side,'conditions':conditions,'analysis':{k:cfg.get('analysis',{}).get(k,True) for k in ('pair_synergy','triple_synergy')},'engine_version':2}
        fingerprint=hashlib.sha256(json.dumps(signature,sort_keys=True).encode()).hexdigest()
        if resume and self.path.is_file():
            self.state=json.loads(self.path.read_text(encoding='utf8'))
            if self.state.get('fingerprint')!=fingerprint:raise ValueError('Adaptive settings differ from this saved run. Restore its configuration or select a new results folder.')
        else:
            old=self.folder/'kingshot_winrates.csv'
            if resume and old.is_file() and old.stat().st_size>250:raise ValueError('This folder has results without an adaptive checkpoint. Use a new folder or turn Resume off.')
            self.state=dict(version=1,fingerprint=fingerprint,mode=self.o['mode'],stage='screen',round=0,queue=[],cursor=0,
                            observations=[],history=[],keep=[],standout=None,seed=int(self.o['seed']),side=side,trials=self.trials,
                            conditions=[list(c) for c in conditions],heroes=self.names,fixed_copies=self.fixed,options=self.o)
        self.rng=np.random.default_rng(self.state['seed'])
        if 'rng' in self.state:self.rng.bit_generator.state=self.state['rng']
        self.folder.mkdir(parents=True,exist_ok=True);self.save()
    def save(self):
        self.state['rng']=self.rng.bit_generator.state;atomic_json(self.path,self.state)
    def recover_csv(self,path,columns):
        # The atomic checkpoint is authoritative; repair a crash between it and CSV append.
        with Path(path).open('w',newline='',encoding='utf8') as f:
            writer=csv.DictWriter(f,fieldnames=columns);writer.writeheader()
            for o in self.state['observations']:writer.writerow(o['row'])
    def screening_fit(self):
        obs=[o for o in self.state['observations'] if o['phase']=='screen']
        idx=np.array([o['index'] for o in obs]);X=self.S[idx];y=np.array([o['row']['winrate']/100 for o in obs])
        V=np.linalg.inv(X.T@X);b=V@X.T@y;r=y-X@b;lev=np.sum((X@V)*X,axis=1)
        noise=np.maximum(.25/self.trials,(r/np.maximum(1-lev,.05))**2)
        cov=V@(X.T@(X*noise[:,None]))@V
        estimates=self.H@b[1:];pairs=list(itertools.combinations(range(self.n),2));beats=np.zeros((self.n,self.n),bool)
        critical=norm.ppf(1-.05/(2*len(pairs)*int(self.o['screen_max_rounds'])))
        for i,j in pairs:
            L=np.r_[0,self.H[i]-self.H[j]];width=critical*math.sqrt(max(float(L@cov@L),0));delta=estimates[i]-estimates[j]
            beats[i,j]=delta>width;beats[j,i]=-delta>width
        ranking=np.argsort(-estimates)
        unresolved=np.flatnonzero(beats.sum(0)<self.target) if self.o['mode']=='fast' else np.flatnonzero(~beats.any(0))
        keep=np.union1d(unresolved,ranking[:self.target]).astype(int).tolist()
        standout=int(ranking[0]) if beats[ranking[0],ranking[1]] else None
        return estimates,keep,standout,V
    def eligible(self):
        keep=self.state['keep'];outside=np.setdiff1d(np.arange(self.n),keep)
        ok=np.all(self.C[:,outside]==0,axis=1)
        pools=self.cfg['joiner'][self.side]['pools'];original={h:int(k) for k,values in pools.items() for h in values}
        for i,name in enumerate(self.names):
            maximum=max(int(self.o['duplicate_limit']),original.get(name,1),int(self.minimum[i]))
            if i==self.state['standout']:maximum=max(maximum,int(self.o['standout_max_copies']))
            ok &= self.C[:,i]+self.minimum[i]<=maximum
        return np.flatnonzero(ok)
    def features(self):
        C=self.C;cols=[np.ones(len(C)),*(C@self.H).T];terms=[('baseline',),*[('contrast',i) for i in range(self.n-1)]];pen=[1e-8]+[.0625]*(self.n-1)
        keep=self.state['keep'];eligible=self.eligible()
        for h in keep:
            for k in range(2,int(C[eligible,h].max())+1):
                cols.append((C[:,h]>=k).astype(float));terms.append(('copy',h,k));pen.append(.25)
        for size,layer in [(2,'pair'),(3,'triple')]:
            if not self.cfg.get('analysis',{}).get(layer+'_synergy',True):continue
            for heroes in itertools.combinations(keep,size):
                cols.append(np.all(C[:,heroes]>0,axis=1).astype(float));terms.append((layer,*heroes));pen.append(2.7777778)
        return np.array(cols).T,terms,np.array(pen)
    def model(self):
        F,terms,pen=self.features();obs=[o for o in self.state['observations'] if o['phase']!='validate'];ids=[o['index'] for o in obs]
        X=F[ids];y=np.array([o['row']['winrate']/100 for o in obs])
        # Scale the prior penalty with batch variance, preserving the benchmark's 100-battle setting.
        V=np.linalg.inv(X.T@X+np.diag(pen*100/self.trials));beta=V@X.T@y
        residual=y-X@beta;noise=max(.25/self.trials,float(np.sum(residual**2)/max(1,len(y)-np.trace(V@(X.T@X)))))
        cov=V*noise
        return F,terms,beta,cov,V
    def set_queue(self,indices):self.state['queue']=[int(x) for x in indices];self.state['cursor']=0;self.save()
    def advance(self):
        stage=self.state['stage']
        if stage=='screen':
            V=None;finish=self.n<=self.target and self.state['round']>0
            if self.state['round']:
                estimates,keep,standout,V=self.screening_fit();self.state['keep']=keep;self.state['standout']=standout
                self.state['history'].append(dict(round=self.state['round'],estimates_pp=(estimates*100).tolist(),retained=[self.names[i] for i in keep],screen_batches=sum(o['phase']=='screen' for o in self.state['observations'])))
                print(f"Adaptive screen round {self.state['round']}: {len(keep)} heroes remain: "+', '.join(self.names[i] for i in keep),flush=True)
                finish=finish or len(keep)<=self.target or self.state['round']>=int(self.o['screen_max_rounds'])
            if not finish:
                self.state['round']+=1
                order=design_order(self.S[self.screen],int(self.o['screen_batches_per_hero'])*self.n,self.rng,V)
                self.set_queue(self.screen[order]);return
            self.state['stage']='refine';eligible=self.eligible()
            if not len(eligible):raise ValueError('No finalist lineup satisfies the fixed slots and copy limits. Use Complete or adjust the constraints.')
            F,_,_,_,V=self.model();budget=int(self.o['refinement_batches_per_hero'])*len(self.state['keep'])
            order=design_order(F[eligible],budget,self.rng,V);self.set_queue(eligible[order])
            print(f'Adaptive refinement: {budget} batches across finalists and their permitted duplicates.',flush=True);return
        if stage=='refine':
            F,terms,beta,cov,_=self.model();pred=np.clip(F@beta,0,1);eligible=self.eligible();ranked=eligible[np.argsort(-pred[eligible])]
            validation_pool=np.union1d(self.screen,eligible)
            total=min(int(self.o['validation_lineups']),len(validation_pool));top_count=min(10,max(1,total//3),len(ranked));selected=list(ranked[:top_count])
            # Explicitly validate a fourth-copy candidate when a standout qualifies and it exists.
            if self.state['standout'] is not None and int(self.o['standout_max_copies'])>=4:
                hero=self.state['standout'];quad=eligible[(self.C[eligible,hero]+self.minimum[hero])==4]
                if len(quad) and int(quad[0]) not in selected and len(selected)<total:selected.append(int(quad[0]))
            remaining=np.setdiff1d(validation_pool,selected)
            selected+=self.rng.choice(remaining,total-len(selected),replace=False).tolist()
            self.state['frozen_model']=dict(prediction=pred.tolist(),beta=beta.tolist(),covariance=cov.tolist(),terms=[list(t) for t in terms],recommended_index=int(ranked[0]))
            self.state['stage']='validate';self.set_queue(np.repeat(selected,int(self.o['validation_batches'])))
            print(f'Frozen model. Independent validation: {total} teams × {self.o["validation_batches"]} batches, including predicted winners.',flush=True);return
        if stage=='validate':
            self.state['stage']='done';self.set_queue([]);print('Adaptive sampling and independent validation complete.',flush=True)
    def batches(self):
        while self.state['stage']!='done':
            if self.state['cursor']>=len(self.state['queue']):self.advance();continue
            index=self.state['queue'][self.state['cursor']];batch=1+sum(o['index']==index for o in self.state['observations'])
            yield index+1,self.conditions[index],[batch]
    def record(self,row):
        index=self.state['queue'][self.state['cursor']]
        self.state['observations'].append(dict(index=index,phase=self.state['stage'],row=copy.deepcopy(row)))
        self.state['cursor']+=1;self.save()
    def finish(self):
        if self.state['stage']=='done':analyze_saved(self.folder)


def analyze_saved(folder,top_n=None):
    """Existing-style plots plus independent validation; validation never enters fitting."""
    import pandas as pd
    import matplotlib.pyplot as plt
    from types import SimpleNamespace
    from scipy.linalg import null_space
    from statsmodels.stats.multitest import multipletests
    import kingshot_full_model_analysis as legacy
    folder=Path(folder);state=json.loads((folder/'adaptive_state.json').read_text(encoding='utf8'))
    if state['stage']!='done':raise ValueError('Adaptive experiment is incomplete. Resume it before creating final analysis plots.')
    snapshot=json.loads((folder/'run_configuration.json').read_text(encoding='utf8'))['configuration']
    plan=AdaptiveExperiment(state['conditions'],snapshot,folder,state['side'],state['trials'],True)
    out=folder/'kingshot_winrates_analysis';out.mkdir(exist_ok=True)
    F,terms,beta,cov,_=plan.model();frozen=state['frozen_model'];pred=np.array(frozen['prediction']);train=[o for o in state['observations'] if o['phase']!='validate'];validation=[o for o in state['observations'] if o['phase']=='validate']
    top_n=int(top_n or snapshot.get('analysis',{}).get('top_n',10));notes=[]
    def grouped(obs):
        groups={}
        for o in obs:groups.setdefault(o['index'],[]).append(o['row']['winrate']/100)
        return groups
    groups=grouped(train);ids=list(groups)
    cond=pd.DataFrame({'lineup_key':[tuple(x for x in plan.conditions[i][plan.offset:plan.offset+4] if x) for i in ids],
                       'p_observed':[float(np.mean(groups[i])) for i in ids],'trials':[len(groups[i])*plan.trials for i in ids]})
    from kingshot_players import player_names
    names=player_names(snapshot,design=snapshot['joiner']);context=[]
    settings_path=folder/'kingshot_winrates_experiment_settings.json'
    if settings_path.is_file():
        saved_names=json.loads(settings_path.read_text(encoding='utf-8-sig')).get('player_names',{})
        for side in ('attacker','defender'):
            if saved_names.get(side):names[side]=str(saved_names[side])
    for side in ('attacker','defender'):
        setup=snapshot['joiner'][side];context.append(f"{names[side]} ({side}): "+'/'.join(setup['leads'].values())+' - '+'-'.join(f'{float(x):g}' for x in setup['troop_percentages']))
    if plan.fixed:context.append('Conditional on mandatory copies: '+', '.join(f'{h} × {v}' for h,v in plan.fixed.items()))
    context.append('Adaptive selection: intervals are approximate; validation is held out.')
    fit=SimpleNamespace(prediction=pred[ids],covariance=np.array(frozen['covariance']))
    legacy.plot_prediction(cond,plan.names,pd.DataFrame(F[ids]),fit,out,top_n,'01_model_prediction_adaptive.png','Regularized finalist interactions',context,plan.trials,plan.side)
    ranked=[]
    eligible=set(plan.eligible().tolist())
    for i in np.argsort(-pred):
        lineup='/'.join(x for x in plan.conditions[i][plan.offset:plan.offset+4] if x)
        ranked.append(dict(condition=int(i+1),lineup=lineup,predicted_pct=float(pred[i]*100),observed_pct=float(np.mean(groups[i])*100) if i in groups else None,batches=len(groups.get(i,[])),eligible_finalist=i in eligible))
    pd.DataFrame(ranked).to_csv(out/'ranked_lineup_predictions.csv',index=False)
    pd.DataFrame([r for r in ranked if r['eligible_finalist']][:top_n]).to_csv(out/'top_model_predicted_lineups.csv',index=False)
    # Descriptive unpenalized estimates only for identifiable contrasts.
    trainids=[o['index'] for o in train];y=np.array([o['row']['winrate']/100 for o in train]);X=F[trainids]
    def ols(A):
        inv=np.linalg.pinv(A);b=inv@y;r=y-A@b;noise=max(.25/plan.trials,float(r@r/max(1,len(y)-np.linalg.matrix_rank(A))))
        return b,inv@inv.T*noise,null_space(A)
    def effect(b,V,N,L):
        if N.size and np.linalg.norm(N.T@L)>1e-7:return None
        v=float(L@b)*100;se=math.sqrt(max(0,float(L@V@L)))*100
        return dict(effect_pp=v,se_pp=se,ci_low=v-1.96*se,ci_high=v+1.96*se,p_value=float(2*norm.sf(abs(v)/se)) if se else 1.)
    basecols=[i for i,t in enumerate(terms) if t[0] in ('baseline','contrast','copy')]
    b,V,N=ols(X[:,basecols]);hero_rows=[]
    for h,name in enumerate(plan.names):
        L=np.zeros(len(basecols));L[1:plan.n]=plan.H[h];e=effect(b,V,N,L)
        if e:hero_rows.append(dict(hero=name,copy=int(plan.minimum[h])+1,term=name+' #'+str(int(plan.minimum[h])+1),**e))
        for pos,col in enumerate(basecols):
            t=terms[col]
            if t[0]=='copy' and t[1]==h:
                contrast=L.copy();contrast[pos]+=1;e=effect(b,V,N,contrast)
                if e:hero_rows.append(dict(hero=name,copy=int(plan.minimum[h])+t[2],term=name+' #'+str(int(plan.minimum[h])+t[2]),**e))
    heroes=pd.DataFrame(hero_rows)
    if len(heroes):legacy.plot_heroes(heroes,out,None,'\n'.join(context));heroes.to_csv(out/'hero_incremental_contributions.csv',index=False)
    else:notes.append('Individual hero contrasts are not independently identifiable in this design.')
    for layer,filename in [('pair','03_pair_synergies_adaptive.png'),('triple','04_triple_synergies_adaptive.png')]:
        included=[i for i,t in enumerate(terms) if layer=='triple' or t[0]!='triple'];B,W,null=ols(X[:,included]);rows=[];missing=0
        for pos,col in enumerate(included):
            term=terms[col]
            if term[0]!=layer:continue
            L=np.zeros(len(included));L[pos]=1;e=effect(B,W,null,L)
            if e is None:missing+=1;continue
            hs=[plan.names[i] for i in term[1:]];rows.append({layer:' + '.join(hs),**dict(zip(['hero_a','hero_b','hero_c'],hs)),**e})
        table=pd.DataFrame(rows)
        (out/filename).unlink(missing_ok=True)
        if len(table):
            table['p_holm']=multipletests(table.p_value,method='holm')[1];table['p_fdr_bh']=multipletests(table.p_value,method='fdr_bh')[1]
            table['significant_holm_0_05']=table.p_holm<.05;table['significant_fdr_0_05']=table.p_fdr_bh<.05
            table.to_csv(out/f'all_{layer}_synergies.csv',index=False)
            if layer=='pair':legacy.plot_synergies(table,SimpleNamespace(constrained=False),out,'all',1,filename,'Finalist pairs only; approximate intervals after adaptive selection.\n'+'\n'.join(context[:2]))
            else:legacy.plot_triple_synergies(table,out,'all',1,filename,'Finalist triples only; approximate intervals after adaptive selection.\n'+'\n'.join(context[:2]))
        else:
            pd.DataFrame(columns=[layer,'effect_pp','se_pp','ci_low','ci_high','p_value']).to_csv(out/f'all_{layer}_synergies.csv',index=False)
        if missing or (not rows and snapshot.get('analysis',{}).get(layer+'_synergy',True)):
            notes.append(f'{layer.title()} synergy: {missing or "all"} requested effects are not independently identifiable from the sampled teams. Unavailable effects are omitted; regularization is used only for prediction.')
    vg=grouped(validation);vids=list(vg);actual=np.array([np.mean(vg[i]) for i in vids]);prediction=pred[vids];mse=float(np.mean((actual-prediction)**2));variance=float(np.var(actual))
    scores=dict(rmse_pp=math.sqrt(mse)*100,r2=1-mse/variance if variance>1e-12 else None,validation_conditions=len(vids),validation_batches=len(validation),training_batches=len(train),total_batches=len(state['observations']),mode=state['mode'],retained_heroes=[plan.names[i] for i in state['keep']],winrate_side=plan.side)
    atomic_json(out/'validation_metrics.json',scores)
    pd.DataFrame([dict(condition=i+1,lineup='/'.join(x for x in plan.conditions[i][plan.offset:plan.offset+4] if x),predicted_pct=pred[i]*100,observed_pct=np.mean(vg[i])*100,batches=len(vg[i])) for i in vids]).to_csv(out/'validation_predictions.csv',index=False)
    fig,ax=plt.subplots(figsize=(9,7));ax.scatter(prediction*100,actual*100);lo=min(actual.min(),prediction.min())*100;hi=max(actual.max(),prediction.max())*100;ax.plot([lo,hi],[lo,hi],color='black')
    ax.set_xlabel(f'Frozen predicted {plan.side} win rate (%)');ax.set_ylabel(f'Independent observed {plan.side} win rate (%)')
    rtext=f'{scores["r2"]:.3f}' if scores['r2'] is not None else 'undefined (no variance)'
    ax.set_title(f'Independent four-joiner validation\nR² = {rtext}; RMSE = {scores["rmse_pp"]:.2f} percentage points\n'+context[0]+'\n'+context[1],fontsize=10)
    fig.tight_layout();fig.savefig(out/'05_independent_validation.png',dpi=180);plt.close(fig)
    atomic_json(out/'analysis_notices.json',dict(fixed_copies=plan.fixed,notes=notes))
    (out/'analysis_summary.txt').write_text('Adaptive four-joiner experiment\n'+json.dumps(scores,indent=2)+'\n\n'+'\n'.join(context)+'\n\n'+'\n'.join(notes)+'\n\nValidation results were excluded from training. R² describes the selected validation set and includes simulation noise. Intervals and post-selection p-values are descriptive approximations, not a familywise confidence guarantee. Unmeasured interactions outside the shortlist are assumed zero in prediction. The broad-ranked prediction CSV contains extrapolations outside the permitted finalist copy limits; only eligible_finalist teams may be recommended.\n',encoding='utf8')
    atomic_json(folder/'adaptive_summary.json',scores)
    print(f'Adaptive validation: {plan.side} win chance; R²={rtext}, RMSE={scores["rmse_pp"]:.2f} pp. Plots: {out}',flush=True)
    if notes:print('KINGSHOT_ANALYSIS_NOTICE:'+json.dumps('\n\n'.join(notes)),flush=True)
    return scores
