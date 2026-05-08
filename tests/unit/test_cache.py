from fleet.cache import task_hash


def test_hash_stable_for_same_input() -> None:
    h1 = task_hash(task="audit svc", scope_paths=["a/b", "c/d"])
    h2 = task_hash(task="audit svc", scope_paths=["a/b", "c/d"])
    assert h1 == h2
    assert len(h1) == 64


def test_hash_path_order_independent() -> None:
    h1 = task_hash(task="t", scope_paths=["a", "b"])
    h2 = task_hash(task="t", scope_paths=["b", "a"])
    assert h1 == h2


def test_hash_whitespace_normalized() -> None:
    h1 = task_hash(task="audit  svc\n", scope_paths=[])
    h2 = task_hash(task="audit svc", scope_paths=[])
    assert h1 == h2


def test_hash_case_normalized() -> None:
    h1 = task_hash(task="Audit Svc", scope_paths=[])
    h2 = task_hash(task="audit svc", scope_paths=[])
    assert h1 == h2


def test_hash_different_task_different_hash() -> None:
    assert task_hash(task="a", scope_paths=[]) != task_hash(task="b", scope_paths=[])
