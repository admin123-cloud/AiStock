from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Event, Lock
import time
import pytest
from services.operations.qmt_download_queue import coordinated_download, DownloadDeferred, protected_session

NIGHT=datetime(2026,9,9,20)


def test_bulk_defers_but_targeted_current_day_is_allowed(tmp_path):
    with pytest.raises(DownloadDeferred):
        coordinated_download(['code']*21,'5m','20260909','20260909',lambda:None,root=tmp_path,now=datetime(2026,9,9,10))
    assert coordinated_download(['code'],'5m','20260909','20260909',lambda:42,root=tmp_path,now=datetime(2026,9,9,10))==42


def test_two_callers_never_download_concurrently(tmp_path):
    guard=Lock(); active=0; peak=0
    def work():
        nonlocal active,peak
        with guard:active+=1;peak=max(peak,active)
        time.sleep(.15)
        with guard:active-=1
    with ThreadPoolExecutor(2) as pool:
        futures=[pool.submit(coordinated_download,[str(i)],'5m','20260101','20260102',work,root=tmp_path,now=NIGHT) for i in range(2)]
        for f in futures:f.result()
    assert peak==1


def test_duplicate_waiter_reuses_finished_download(tmp_path):
    entered=Event(); release=Event();calls=[]
    def work():calls.append(1);entered.set();release.wait(2)
    with ThreadPoolExecutor(2) as pool:
        first=pool.submit(coordinated_download,['a'],'5m','20260101','20260102',work,root=tmp_path,now=NIGHT)
        assert entered.wait(1)
        second=pool.submit(coordinated_download,['a'],'5m','20260101','20260102',work,root=tmp_path,now=NIGHT)
        time.sleep(.15);release.set();first.result();second.result()
    assert len(calls)==1


def test_failed_call_releases_owner(tmp_path):
    def fail():raise ValueError('source unavailable')
    with pytest.raises(ValueError):coordinated_download(['a'],'5m','20260101','20260102',fail,root=tmp_path,now=NIGHT)
    assert coordinated_download(['b'],'5m','20260101','20260102',lambda:1,root=tmp_path,now=NIGHT)==1
