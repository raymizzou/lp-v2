"""One-command live control CLI, separate from the frozen run/stop interface."""
import argparse
import json
from lp_v2.live_control import profiles, workflow


def main(argv):
    parser=argparse.ArgumentParser(prog='lp-v2 live')
    commands=parser.add_subparsers(dest='command',required=True)
    check=commands.add_parser('check'); check.add_argument('--profile')
    on=commands.add_parser('on'); on.add_argument('--profile'); on.add_argument('--action-id'); on.add_argument('--window-end'); on.add_argument('--prepare-run-env',action='store_true')
    off=commands.add_parser('off'); off.add_argument('--profile'); off.add_argument('--action-id')
    setup=commands.add_parser('setup'); setup.add_argument('--profile',required=True)
    setup.add_argument('--ssh-host'); setup.add_argument('--capital-usdc'); setup.add_argument('--market'); setup.add_argument('--set-default',action='store_true'); setup.add_argument('--release-root',default='/opt/lp-v2')
    group=commands.add_parser('profile').add_subparsers(dest='operation',required=True)
    show=group.add_parser('show'); show.add_argument('name')
    default=group.add_parser('set-default'); default.add_argument('name')
    change=group.add_parser('set'); change.add_argument('name')
    for option in profiles.SET_FIELDS:
        change.add_argument('--'+option.replace('_','-'))
    args=parser.parse_args(argv)
    try:
        if args.command=='setup':
            profile=profiles.setup(args.profile,args.ssh_host,capital=args.capital_usdc,market=args.market,default=args.set_default,release_root=args.release_root)
            missing=profiles.validate(profile)
            result=dict(status='SAVED_DRAFT' if missing else 'SAVED',local_saved=True,name=profile['name'],
                        revision=profile['revision'],missing=missing,can_enable=False,default_saved=bool(args.set_default and not missing))
            if missing:
                code=2
            else:
                try:result['check']=workflow.check(profile)
                except Exception:result['check']=dict(status='UNKNOWN',reason='CHECK_UNAVAILABLE')
                result['can_enable']=result['check'].get('status')=='PASS'
                code=0
        elif args.command=='profile':
            if args.operation=='set':
                profile=profiles.edit(args.name,vars(args))
            elif args.operation=='set-default':
                profile=profiles.set_default(args.name)
            else:
                profile=profiles.load(args.name)
            result=dict(profile=profile,missing=profiles.validate(profile))
            code=0
        elif args.command=='off':
            result=workflow.off(profiles.load(args.profile),action_id=args.action_id)
            code=0 if result.get('status')=='PASS' else 2
        elif args.command=='on':
            profile=profiles.load(args.profile); profiles.validate(profile,complete=True)
            result=workflow.on(profile,action_id=args.action_id,window_end=args.window_end,prepare_env=args.prepare_run_env)
            code=0 if result.get('status')=='PASS' else 2
        else:
            result=workflow.check(profiles.load(args.profile))
            code=0 if result.get('status')=='PASS' else 2
        if args.command in ('check','on','off'):
            result['runtime_acceptance']={'merge':{'status':'NOT_RUN','reason':'REQUIRES_RUNTIME_TRANSACTION_EVIDENCE'}}
        print(json.dumps(result))
        return code
    except Exception as error:
        reason=str(error) if isinstance(error,profiles.ProfileError) else 'PROFILE_INVALID'
        print(json.dumps(dict(status='UNKNOWN',reason=reason)))
        return 2
