from app.contracts import instant, load, require_tables, visible
from app.domain import OPERATIONS, TABLES, action
from app.repository import Repository


class Service:
    def __init__(self, source, database):
        self.source = source
        self.repository = Repository(database)

    def run(self, request):
        if not isinstance(request, dict):
            raise ValueError('分析请求须为对象')
        if not isinstance(request.get('request_id'), str) or not request['request_id'].strip():
            raise ValueError('缺少请求编号')
        operation = request.get('operation')
        if operation not in OPERATIONS:
            raise ValueError('未知分析类型')
        instant(request['as_of'])
        previous = self.repository.previous(request['request_id'], request)
        if previous is not None:
            return previous
        dataset = load(self.source)
        require_tables(dataset, TABLES)
        snapshot = visible(dataset, request['as_of'])
        result = OPERATIONS[operation](snapshot, request.get('options', {}))
        return self.repository.remember(request['request_id'], request, snapshot, result)

    def apply(self, request):
        return action(self.repository, request)

    def describe(self):
        return {'operations': sorted(OPERATIONS), 'tables': TABLES}
