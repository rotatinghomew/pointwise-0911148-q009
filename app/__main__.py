TITLE = '复核供应商一致性台'
OPERATION = 'assignment_overlap'
CONSENSUS_OPERATION = 'category_consensus'

import argparse
import json

from app.httpd import create_server
from app.service import Service


def main():
    parser = argparse.ArgumentParser(description=TITLE)
    parser.add_argument('--source', default='data/source.json')
    parser.add_argument('--database', default='var/analyses.sqlite3')
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('inspect', help='保存业务覆盖分析')
    run.add_argument('--as-of', default='2026-09-30T23:59:59Z')
    run.add_argument('--request-id', default='coverage-september')
    consensus = commands.add_parser('consensus', help='保存类别共识与供应商可靠度分析')
    consensus.add_argument('--as-of', default='2026-09-30T23:59:59Z')
    consensus.add_argument('--request-id', default='consensus-current')
    consensus.add_argument('--supersedes', default=None,
                          help='接续哪个既有分析编号；旧版本保留不变')
    consensus.add_argument('--min-anchors', type=int, default=None)
    consensus.add_argument('--expert-slots', type=int, default=None)
    consensus.add_argument('--summary', action='store_true', help='只输出续约讨论视图')
    server = commands.add_parser('serve', help='启动本地分析接口')
    server.add_argument('--host', default='127.0.0.1')
    server.add_argument('--port', type=int, default=8080)
    commands.add_parser('demo', help='展示当前覆盖分析')
    args = parser.parse_args()
    service = Service(args.source, args.database)
    if args.command == 'serve':
        instance = create_server(service, args.host, args.port)
        print(json.dumps({'地址': instance.server_address}, ensure_ascii=False), flush=True)
        try:
            instance.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            instance.server_close()
    elif args.command == 'consensus':
        options = {}
        if args.min_anchors is not None:
            options['min_anchors'] = args.min_anchors
        if args.expert_slots is not None:
            options['expert_slots'] = args.expert_slots
        request = {'operation': (CONSENSUS_OPERATION + '_summary' if args.summary
                                 else CONSENSUS_OPERATION),
                   'request_id': args.request_id, 'as_of': args.as_of,
                   'options': options}
        if args.supersedes:
            request['supersedes'] = args.supersedes
        print(json.dumps(service.run(request), ensure_ascii=False, indent=2))
    elif args.command == 'demo':
        as_of = '2026-09-30T23:59:59Z'
        coverage = service.run({'operation': OPERATION, 'request_id': 'demo-current',
                                'as_of': as_of})
        consensus = service.run({'operation': CONSENSUS_OPERATION + '_summary',
                                 'request_id': 'demo-consensus', 'as_of': as_of})
        consensus['attention'] = consensus['attention'][:10]
        consensus['attention_note'] = ('完整逐资料建议、来源与校准诊断见 category_consensus 记录；'
                                       '演示只打印前 10 条关注资料。')
        print(json.dumps({'覆盖核对': coverage, '类别共识摘要': consensus},
                         ensure_ascii=False, indent=2))
    else:
        request = {'operation': OPERATION, 'request_id': getattr(args, 'request_id', 'demo-current'),
                   'as_of': getattr(args, 'as_of', '2026-09-30T23:59:59Z')}
        print(json.dumps(service.run(request), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
