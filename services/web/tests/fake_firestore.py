"""A small in-memory stand-in for google.cloud.firestore.Client, enough for FirestoreRepo.

It checks OUR logic (queries, mutate, lock, slots) on every run. It cannot prove Firestore's own behaviour:
that needs a real project or the emulator (which needs Java). See the README, 'Verified how'.
"""

import copy
import itertools

_ids = itertools.count(1)


class Snap:
    def __init__(self, ref, data):
        self.reference, self._data, self.exists = ref, data, data is not None

    @property
    def id(self):
        return self.reference.id

    def to_dict(self):
        return copy.deepcopy(self._data) if self._data is not None else None


class Ref:
    def __init__(self, col, doc_id):
        self.col, self.id = col, doc_id

    def get(self, transaction=None):
        return Snap(self, self.col.docs.get(self.id))

    def set(self, data, merge=False):
        if merge and self.id in self.col.docs:
            self.col.docs[self.id].update(copy.deepcopy(data))
        else:
            self.col.docs[self.id] = copy.deepcopy(data)

    def update(self, data):
        if self.id not in self.col.docs:
            from google.api_core.exceptions import NotFound

            raise NotFound(self.id)  # what the real client raises for an update of a missing document
        self.col.docs[self.id].update(copy.deepcopy(data))

    def delete(self):
        self.col.docs.pop(self.id, None)


class Query:
    def __init__(self, col, filters):
        self.col, self.filters = col, filters

    def where(self, filter):
        return Query(self.col, self.filters + [filter])

    def stream(self, transaction=None):
        for doc_id, data in list(self.col.docs.items()):
            if all(f.op_string == "==" and data.get(f.field_path) == f.value for f in self.filters):
                yield Snap(Ref(self.col, doc_id), data)


class Col(Query):
    def __init__(self, name):
        self.name, self.docs = name, {}
        super().__init__(self, [])

    def document(self, doc_id=None):
        return Ref(self, doc_id or f"auto{next(_ids):08d}")


class Txn:
    def set(self, ref, data):
        ref.set(data)

    def update(self, ref, data):
        ref.update(data)

    def delete(self, ref):
        ref.delete()


class FakeClient:
    def __init__(self):
        self.cols: dict[str, Col] = {}

    def collection(self, name):
        return self.cols.setdefault(name, Col(name))

    def transaction(self):
        return Txn()
