import time
from assistant import brain
from tests.test_assistant_brain import *  # noqa  fixtures

def test_diag(env):
    def reply(said, client):
        if said == "tell me a long story":
            return [f"Sentence number {i}." for i in range(200)]
        return ["Here is the short answer."]
    brain.set_client_factory(lambda o, t: brain.FakeClient(o, t, lambda p, c: reply(
        p.split("<said>")[-1].split("</said>")[0].strip(), c), delay=0.01))
    T0=time.monotonic(); t1 = brain.ask("tell me a long story")
    for _ in range(200):
        if t1.sentences:
            break
        time.sleep(0.01)
    b = brain._brain
    cur = b.current
    info = dict(el=round(time.monotonic()-T0,2), cur=cur and cur.id, t1=t1.id, n=len(t1.sentences), first=t1.sentences[:2], done=t1.done.is_set(),
                fin=t1.finished and t1.finished.is_set(), err=t1.error_text, rec=getattr(t1,'recovered',None),
                same=b is brain.get_brain(), kind=t1.kind)
    ok = brain.interrupt()
    assert ok, repr(info)
