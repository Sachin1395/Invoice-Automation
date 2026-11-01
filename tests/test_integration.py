"""End-to-end: drop files in the inbox, run the runner, check DB + folders + pages."""
from dataclasses import replace
from pathlib import Path

from PIL import Image

from invoice_pipeline import db
from invoice_pipeline.models import JobStatus
from invoice_pipeline.runner import run_once

from conftest import (
    fake_exe_bytes, make_encrypted_pdf, make_heic, make_huge_jpeg, make_rotated_jpeg,
    make_scanned_pdf, make_text_pdf, make_transparent_png, make_truncated_jpeg,
)


def by_name(results):
    return {r.filename: r for r in results}


def test_full_fixture_folder(settings, drop):
    drop("a_text.pdf", make_text_pdf(pages=2))
    drop("b_scan.pdf", make_scanned_pdf())
    drop("c_phone.jpg", make_rotated_jpeg())
    drop("d_logo.png", make_transparent_png())
    drop("e_iphone.heic", make_heic())
    drop("f_huge.jpg", make_huge_jpeg())
    drop("g_fake.pdf", fake_exe_bytes())
    drop("h_locked.pdf", make_encrypted_pdf())
    drop("i_broken.jpg", make_truncated_jpeg())
    drop("j_many.pdf", make_text_pdf(pages=16))

    res = by_name(run_once(settings))

    for ok in ("a_text.pdf", "b_scan.pdf", "c_phone.jpg", "d_logo.png", "e_iphone.heic", "f_huge.jpg"):
        assert res[ok].status == JobStatus.DONE, (ok, res[ok])
    assert res["g_fake.pdf"].detail == "unsupported_type"
    assert res["h_locked.pdf"].detail == "encrypted_pdf"
    assert res["i_broken.jpg"].detail == "corrupt_file"
    assert res["j_many.pdf"].detail == "too_many_pages"

    # folders reflect outcomes; inbox is empty
    assert not list(settings.inbox_dir.iterdir())
    assert len(list(settings.processed_dir.iterdir())) == 6
    assert len(list(settings.rejected_dir.iterdir())) == 4

    # page images exist, respect the size cap, and the phone photo was rotated upright
    with db.session(settings.db_path) as conn:
        n_files = conn.execute("SELECT COUNT(*) c FROM files").fetchone()["c"]
        text_job = db.get_job(conn, res["a_text.pdf"].job_id)
        phone_job = db.get_job(conn, res["c_phone.jpg"].job_id)
        huge_job = db.get_job(conn, res["f_huge.jpg"].job_id)
    assert n_files == 6

    assert len(list((settings.pages_dir / text_job["sha256"]).glob("page_*.jpg"))) == 2
    with Image.open(settings.pages_dir / phone_job["sha256"] / "page_001.jpg") as im:
        assert im.size[0] < im.size[1]  # portrait after EXIF fix
    with Image.open(settings.pages_dir / huge_job["sha256"] / "page_001.jpg") as im:
        assert max(im.size) == 2048


def test_duplicate_short_circuits(settings, drop):
    data = make_text_pdf()
    drop("first.pdf", data)
    first = run_once(settings)[0]
    assert first.status == JobStatus.DONE

    drop("again.pdf", data)  # same bytes, different name
    second = run_once(settings)[0]
    assert second.status == JobStatus.DUPLICATE
    assert second.detail == f"duplicate of job {first.job_id}"

    with db.session(settings.db_path) as conn:
        assert db.get_job(conn, second.job_id)["duplicate_of"] == first.job_id
        assert conn.execute("SELECT COUNT(*) c FROM files").fetchone()["c"] == 1


def test_rejected_file_can_be_resubmitted_only_done_blocks(settings, drop):
    """A rejected earlier job must not block a later submission of the same bytes."""
    data = make_text_pdf(pages=5)
    strict = replace(settings, max_pages=3)
    drop("big.pdf", data)
    assert run_once(strict)[0].status == JobStatus.REJECTED

    drop("big_again.pdf", data)  # now with normal limits
    assert run_once(settings)[0].status == JobStatus.DONE


def test_strictly_sequential_fifo(settings, drop):
    import os, time
    for i, name in enumerate(["one.pdf", "two.pdf", "three.pdf"]):
        p = drop(name, make_text_pdf(text=f"invoice number {i} " * 5))
        os.utime(p, (time.time() + i, time.time() + i))  # force mtime order
    results = run_once(settings)
    assert [r.filename for r in results] == ["one.pdf", "two.pdf", "three.pdf"]
    assert [r.job_id for r in results] == sorted(r.job_id for r in results)


def test_empty_inbox(settings):
    assert run_once(settings) == []
