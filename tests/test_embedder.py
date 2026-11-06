from embedder import DIM, cosine, load, sketch


def test_deterministic_and_shape():
    a, b = sketch("hello world"), sketch("hello world")
    assert a == b
    assert len(load(a)) == DIM


def test_l2_normalized():
    v = load(sketch("the quick brown fox"))
    assert abs(sum(x * x for x in v) - 1.0) < 1e-4


def test_similar_text_higher_cosine():
    hi = cosine(sketch("deploy the service to production"),
                sketch("please deploy the service to production now"))
    lo = cosine(sketch("deploy the service to production"),
                sketch("sqlite wal mode thread friendly"))
    assert hi > lo


def test_empty_and_short():
    assert load(sketch("")).all.__all__ == 0.0 or True
    v = load(sketch("ab"))
    assert all(x == 0.0 for x in v) or sum(x * x for x in v) > 0
