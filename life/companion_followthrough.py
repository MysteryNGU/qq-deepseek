"""A project's one durable continuation, independent of the conversation idle timer."""
import time


FIELD = dict(type='object',additionalProperties=False,required=['mode'],properties={
    'mode':{'enum':['conversation','act','wait']},
    'project':{'type':'string'},'reason':{'type':'string','maxLength':600}})


class FollowThroughError(ValueError):
    pass


class FollowThrough:
    def __init__(self,core,delay=15):
        self.core,self.repo,self.tasks,self.scope=core,core.repo,core.tasks,core.scope
        self.delay=delay

    def _ref(self,key):
        return self.core._project_ref(key)

    def _project(self,ref):
        return self.tasks.get(ref,self.scope.scope_id)

    def review_interval(self):
        """等待项目的复核间隔：比普通续行慢，避免在没变化时反复空转。"""
        return max(float(self.delay)*4, 60.0)

    def _busy(self,ref):
        return bool(self.repo._one("SELECT 1 FROM companion_actions WHERE agent=? AND project_ref=? "
            "AND status IN ('pending','running') AND json_extract(action,'$.kind')!='body' LIMIT 1",
            (self.core.life.agent_id,ref)))

    def _save(self,project,state,commitment=None):
        links=dict(continuation=state)
        if commitment is not None:
            links['commitment']=commitment
        self.tasks.ensure(self.scope,project['ref'],None,**links)

    def validate(self,decision,*,continuation_ref=None,delegated_refs=()):
        """Validate intention against actions; never infer a promise by matching prose.

        delegated_refs must come from actual in-flight external operations, not the model.
        Missing engagement remains compatible with old fixtures; a continuation still
        cannot silently produce another plan without an action or an explicit wait.
        """
        changes={self._ref(p['key']):p for p in decision.get('projects',[])}
        engagement=decision.get('engagement') or {}
        mode=engagement.get('mode','conversation')
        if mode not in {'conversation','act','wait'}:
            raise FollowThroughError('engagement.mode无效')
        project_key=engagement.get('project','')
        selected=self._ref(project_key) if project_key else None
        delegated=set(delegated_refs)
        def effective(ref):
            return {**(self._project(ref) or {}),**changes.get(ref,{})}
        def actionable(ref):
            return (any(a.get('kind')!='body' and self._ref(a.get('project',''))==ref
                        for a in decision.get('actions',[])) or self._busy(ref) or ref in delegated)
        def explicit_wait(ref):
            project=effective(ref)
            return bool(mode=='wait' and selected==ref and str(engagement.get('reason','')).strip()
                and (project.get('status')=='paused' or
                     (project.get('status')=='waiting' and (project.get('blocked_on') or project.get('wait_reason'))) or
                     ref in delegated or project.get('continuation',{}).get('state')=='waiting_result'))
        if mode=='wait' and (not selected or effective(selected).get('status') in {'done','cancelled'}):
            mode='conversation'
            engagement['mode']=mode
            engagement.pop('project',None)
            decision['engagement']=engagement
        if mode in {'act','wait'} and not selected:
            raise FollowThroughError('act/wait需要明确engagement.project')
        if mode=='act' and not actionable(selected):
            # A body expression is real work, but a smile cannot advance a separately
            # tracked persistent project. When the only work this turn is a body action,
            # keep the expression and the reply: downgrade to ordinary conversation
            # instead of rejecting the whole turn, which used to withhold the text too.
            # Removing engagement.project is required: record() would otherwise
            # reschedule a same-named long-term project's continuation from a smile.
            body_only=any(a.get('kind')=='body' and self._ref(a.get('project',''))==selected
                          for a in decision.get('actions',[]))
            declaring_continue=False
            if body_only:
                pass
            elif selected in changes and str(changes[selected].get('next_step') or '').strip():
                # 续行轮里她说"我接着做"，并写下下一步：这是明确的继续意图，
                # 程序会把这件事实排进持久续行，不需要她这一轮就调用工具。
                project_now=effective(selected)
                if project_now.get('status')!='active' or not str(project_now.get('next_step','')).strip():
                    raise FollowThroughError('声明继续需要项目保持active并写明下一步')
                declaring_continue=True
            else:
                raise FollowThroughError('act需要同项目实际行动、在途执行或真实外部委派，不能只有回复和计划')
            if not declaring_continue:
                mode='conversation'
                engagement['mode']='conversation'
                engagement.pop('project',None)
                decision['engagement']=engagement
        if mode=='wait' and not explicit_wait(selected):
            raise FollowThroughError('wait需要明确理由，并将项目标为有阻碍的waiting或自主休息paused')
        if continuation_ref:
            project=effective(continuation_ref)
            terminal=project.get('status') in {'done','cancelled'}
            if not terminal and not actionable(continuation_ref) and not explicit_wait(continuation_ref):
                # 她被叫回来说话，本来就可以只回一句真实进展：
                # 那既不是"改计划"，也不该被当成提交错误丢掉。
                # 只有当真在改动项目计划却没有行动/等待时才算"只更新计划"。
                changed = continuation_ref in changes
                said = bool(str(decision.get('reply') or '').strip() or decision.get('followups'))
                # 明确声明继续（act + 同一项目 + 有效下一步）是持久续行安排，
                # 不算"只更新计划"。
                declaring = bool(engagement.get('mode') == 'act'
                                 and self._ref(engagement.get('project', '')) == continuation_ref
                                 and project.get('status') == 'active'
                                 and str(project.get('next_step') or '').strip())
                if declaring:
                    pass
                elif changed or not said:
                    raise FollowThroughError('本次是项目续行：请实际行动，或明确记录等待/休息；不能再只更新计划')

    def record(self,decision,source_id='',*,now=None,continuation_ref=None,delegated_refs=()):
        """Call after project changes/actions are committed, inside their transaction.

        A plain chat touches no project and cannot postpone any due time. A claimed
        continuation is not automatically replenished merely by rewording its plan.
        """
        now=time.time() if now is None else now
        changes={self._ref(p['key']):p for p in decision.get('projects',[])}
        refs=set(changes)
        refs.update(self._ref(a['project']) for a in decision.get('actions',[]) if a.get('kind')!='body')
        engagement=decision.get('engagement') or {}
        if engagement.get('project'):refs.add(self._ref(engagement['project']))
        if continuation_ref:refs.add(continuation_ref)
        delegated=set(delegated_refs)
        for ref in refs:
            project=self._project(ref)
            if not project or project.get('ephemeral'):continue
            old=project.get('continuation') or {}
            if (old.get('external') and old.get('state')=='scheduled'
                    and project.get('status') in {'active','waiting'}):
                # A receipt may arrive while an earlier accepted-result assessment
                # is still generating its waiting decision. Only claim() proves the
                # new receipt has actually been presented for assessment.
                continue
            state=dict(old,updated=now,source_id=source_id or old.get('source_id',''))
            if project.get('status')!='active' or not str(project.get('next_step','')).strip():
                state.update(state='stopped',due_at=None)
            elif self._busy(ref) or ref in delegated:
                state.update(state='waiting_result',due_at=None,declarations=0)
            elif old.get('state')=='waiting_result':
                # Accepted/queued is not completion; the real result must release this.
                continue
            elif ref==continuation_ref:
                change=changes.get(ref) or {}
                engagement=decision.get('engagement') or {}
                explicit=bool(
                    (engagement.get('mode')=='act'
                     and self._ref(engagement.get('project',''))==ref)
                    or (change.get('status','active')=='active'
                        and str(change.get('next_step','')).strip()))
                declarations=int(old.get('declarations') or 0)+1
                if explicit and project.get('status')=='active' and declarations<=3:
                    # 明确选择继续：必须落到持久续行，不能等普通自主轮碰巧想起。
                    delay=float(decision.get('wake_after') or self.delay)
                    state.update(state='scheduled',due_at=now+delay,
                                 next_step=project['next_step'],claims=0,
                                 declarations=declarations)
                elif explicit:
                    # 反复声明继续却一直没有实际执行：排期成功不等于续行完成。
                    state.update(state='stopped',due_at=None,declarations=declarations,
                                 parked_reason='连续声明继续但没有执行，转正常自主轮')
                else:
                    # 只是回了一句、或选择稍后/等待：保留原因，不替她排时间。
                    state.update(state='stopped',due_at=None,
                                 parked_reason='本轮只表达，没有给出下一步；留给正常自主轮')
            elif old.get('state')=='claimed' and old.get('next_step')==project['next_step']:
                continue
            else:
                due=old.get('due_at')
                # A new self-chosen activity shares the character's chosen rhythm.
                # Existing commitments keep their due time across unrelated chat.
                chosen_timing=(project.get('origin')=='self' or
                    (engagement.get('mode')=='act' and engagement.get('project')
                     and self._ref(engagement['project'])==ref))
                delay=(decision.get('wake_after') or self.delay) if chosen_timing else self.delay
                state.update(state='scheduled',due_at=due if isinstance(due,(int,float)) else now+delay,
                             next_step=project['next_step'],claims=0)
            commitment=project.get('commitment')
            if (commitment and commitment.get('review_at')
                    and state.get('state') in {'scheduled','claimed','waiting_result'}):
                # 复核时间用过一次就清掉，否则每一拍都会被当成"又到点了"。
                commitment=dict(commitment,review_at=None,last_reviewed_at=now)
            else:
                commitment=None
            self._save(project,state,commitment)

    def due(self,*,now=None,autonomy=True,delegated_refs=()):
        now=time.time() if now is None else now
        candidates=[]
        for project in self.core._projects():
            state=project.get('continuation') or {}
            commitment=project.get('commitment') or {}
            external_wait=project.get('status')=='waiting' and bool(state.get('external'))
            # 约定到点复核：waiting 的承诺不会自己变成 active，
            # 但到了约定的复核时间就应该重新被想起，否则它只是躺着的一行数据。
            review_at=commitment.get('review_at')
            # 她自己选的复核时间：到点只是让这件事重新进入注意力，
            # 不改变项目状态，也不强制她去执行。
            self_review_at=project.get('review_at')
            user_stopped=(project.get('pause_owner')=='user'
                          or project.get('status')=='cancelled')
            self_review=bool(not user_stopped and project.get('status') in {'waiting','paused'}
                             and isinstance(self_review_at,(int,float)) and self_review_at<=now)
            review_due=bool(not user_stopped and ((project.get('status')=='waiting' and not commitment.get('fulfilled_at')
                             and isinstance(review_at,(int,float)) and review_at<=now
                            and state.get('state')!='claimed') or self_review))
            if (project.get('status')!='active' and not external_wait
                    and not review_due) or project.get('ephemeral'):
                continue
            if not autonomy and project.get('origin')!='user':continue
            if not str(project.get('next_step','')).strip():continue
            if review_due:
                pass
            elif state.get('state')!='scheduled' or not isinstance(state.get('due_at'),(int,float)) or state['due_at']>now:
                continue
            ref=project['ref']
            # The waiting project may inspect a real new receipt, while a stopped
            # ancestor must still block it. Neither lifecycle is changed here.
            waiting_review=bool(external_wait or review_due)
            blocker_ref=project.get('parent_ref') if waiting_review else ref
            if (ref in delegated_refs and not state.get('external')) or (blocker_ref and self.tasks.blocker(blocker_ref,self.scope.scope_id)) or self._busy(ref):continue
            due_at=(self_review_at if self_review else review_at) if review_due else state.get('due_at')
            candidates.append(dict(ref=ref,key=project.get('key',ref),goal=project.get('goal',''),
                source_id=state.get('source_id',''),external=bool(state.get('external')),
                promise=commitment.get('promise',''),
                review=bool(review_due),
                waiting=bool(project.get('status')!='active'),
                blocked_on=project.get('blocked_on') or project.get('wait_reason') or '',
                last_receipt=project.get('last_receipt') or {},
                next_step_set_at=project.get('next_step_set_at') or 0,
                next_step=project.get('next_step',''),due_at=due_at,origin=project.get('origin')))
        return min(candidates,key=lambda p:(p['due_at'],p['ref'])) if candidates else None

    def claim(self,ref,*,now=None,autonomy=True,delegated_refs=()):
        """Consume before cognition; a crash/invalid/no-action response cannot loop."""
        now=time.time() if now is None else now
        with self.repo.tx():
            chosen=self.due(now=now,autonomy=autonomy,delegated_refs=delegated_refs)
            if not chosen or chosen['ref']!=ref:return False
            project=self._project(ref)
            commitment=project.get('commitment') or {}
            if commitment.get('review_at'):
                commitment=dict(commitment,review_at=None,last_reviewed_at=now)
            else:
                commitment=None
            if isinstance(project.get('review_at'),(int,float)):
                self.tasks.ensure(self.scope,ref,None,review_at=None,last_reviewed_at=now)
            # 复核候选可能还没排过期，continuation 尚不存在。
            self._save(project,dict(project.get('continuation') or {},
                                    state='claimed',due_at=None,updated=now),commitment)
        return True

    def after_result(self,ref,result,*,now=None,source_id='',external=False):
        """Schedule once from a real executor receipt; accepted waits for real completion.

        Call BEFORE post-action cognition. Its successful record() may replace this
        fallback with new execution/wait; if cognition fails, the due remains durable.
        """
        now=time.time() if now is None else now
        project=self._project(ref)
        allowed={'active','waiting'} if external else {'active'}
        if not project or project.get('status') not in allowed or not project.get('next_step') or project.get('ephemeral'):return
        if external and not source_id:return
        if not isinstance(result,dict) or not result.get('status'):return
        old=project.get('continuation') or {}
        if external and old.get('last_external_source_id')==source_id:return
        waiting=result.get('completion') in {'accepted','queued'} or result['status'] in {'accepted','queued','pending','running'}
        delegation=result.get('delegation_id')
        if not external and waiting and delegation and delegation==old.get('last_external_delegation_id'):
            # Callback may beat execute()'s accepted return. That late acknowledgement
            # cannot erase an already observed receipt or rearm a consumed one.
            return
        state=dict(old,updated=now,next_step=project['next_step'],external=external,
                   source_id=source_id or old.get('source_id',''))
        if external:
            state['last_external_source_id']=source_id
            state['last_external_delegation_id']=delegation
        old_due=old.get('due_at')
        due=min(old_due,now+self.delay) if isinstance(old_due,(int,float)) else now+self.delay
        state.update(state='waiting_result' if waiting else 'scheduled',due_at=None if waiting else due)
        self._save(project,state)
