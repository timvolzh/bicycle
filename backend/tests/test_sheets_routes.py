import sys
from pathlib import Path
import asyncio
import pytest
from httpx import AsyncClient, ASGITransport
from datetime import datetime
from bson import ObjectId

# add backend root to path
ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT))

from app.main import app
from app.utils import get_db, get_current_user


class FakeInsertOneResult:
    def __init__(self, inserted_id):
        self.inserted_id = inserted_id


class FakeDeleteResult:
    def __init__(self, deleted_count):
        self.deleted_count = deleted_count


class FakeCursor:
    def __init__(self, docs):
        self.docs = list(docs)

    def sort(self, key, direction):
        reverse = direction == -1
        self.docs.sort(key=lambda d: d.get(key), reverse=reverse)
        return self

    async def to_list(self, length):
        return self.docs[:length]


class FakeCollection:
    def __init__(self):
        self.store = {}

    async def insert_one(self, doc):
        _id = doc.get('_id', ObjectId())
        doc['_id'] = _id
        self.store[_id] = doc
        return FakeInsertOneResult(_id)

    def find(self, filter=None):
        filter = filter or {}
        def match(doc):
            return all(doc.get(k) == v for k, v in filter.items())
        return FakeCursor([d for d in self.store.values() if match(d)])

    async def find_one(self, filter):
        for doc in self.store.values():
            if all(doc.get(k) == v for k, v in filter.items()):
                return doc
        return None

    async def find_one_and_update(self, filter, update, return_document=False):
        doc = await self.find_one(filter)
        if not doc:
            return None
        if '$set' in update:
            doc.update(update['$set'])
        if return_document:
            return doc
        return None

    async def delete_one(self, filter):
        for _id, doc in list(self.store.items()):
            if all(doc.get(k) == v for k, v in filter.items()):
                del self.store[_id]
                return FakeDeleteResult(1)
        return FakeDeleteResult(0)


def create_test_db():
    class FakeDatabase:
        def __init__(self):
            self.sheets = FakeCollection()
    return FakeDatabase()


def override_dependencies(test_db):
    async def override_get_db():
        return test_db

    async def override_get_current_user():
        return {'username': 'testuser'}

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user


def test_create_sheet():
    async def run():
        db = create_test_db()
        override_dependencies(db)
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            payload = {'payload': {'data': {'name': {'value': 'Hero'}}}, 'name': 'Hero'}
            resp = await client.post('/api/v1/sheets/', json=payload)
            assert resp.status_code == 200
            data = resp.json()
            assert data['name'] == 'Hero'
            assert data['owner'] == 'testuser'
            assert data['payload'] == payload['payload']
            assert '_id' in data and 'created_at' in data
        app.dependency_overrides.clear()
    asyncio.run(run())


def test_list_sheets_owned_by_user():
    async def run():
        db = create_test_db()
        override_dependencies(db)
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            for idx in range(2):
                await client.post('/api/v1/sheets/', json={'payload': {'n': idx}, 'name': f'S{idx}'})
            await db.sheets.insert_one({'payload': {}, 'name': 'other', 'owner': 'other', 'created_at': datetime.utcnow()})
            resp = await client.get('/api/v1/sheets/')
            assert resp.status_code == 200
            data = resp.json()
            assert len(data) == 2
            assert {d['name'] for d in data} == {'S0', 'S1'}
        app.dependency_overrides.clear()
    asyncio.run(run())


def test_patch_sheet_and_invalid_id():
    async def run():
        db = create_test_db()
        override_dependencies(db)
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            created = (await client.post('/api/v1/sheets/', json={'payload': {'v': 1}, 'name': 'old'})).json()
            sheet_id = created['_id']
            new_payload = {'payload': {'v': 2}, 'name': 'new'}
            resp = await client.patch(f'/api/v1/sheets/{sheet_id}', json=new_payload)
            assert resp.status_code == 200
            data = resp.json()
            assert data['name'] == 'new'
            assert data['payload'] == new_payload['payload']
            resp_bad = await client.patch('/api/v1/sheets/badid', json=new_payload)
            assert resp_bad.status_code == 404
        app.dependency_overrides.clear()
    asyncio.run(run())


def test_delete_sheet():
    async def run():
        db = create_test_db()
        override_dependencies(db)
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            created = (await client.post('/api/v1/sheets/', json={'payload': {'x': 1}, 'name': 'tmp'})).json()
            sheet_id = created['_id']
            resp = await client.delete(f'/api/v1/sheets/{sheet_id}')
            assert resp.status_code == 200
            assert resp.json() == {'ok': True}
            resp_list = await client.get('/api/v1/sheets/')
            assert resp_list.status_code == 200
            assert resp_list.json() == []
        app.dependency_overrides.clear()
    asyncio.run(run())
