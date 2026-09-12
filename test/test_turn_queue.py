import threading

import pytest

from overlay.bubble.turn_queue import MIB, TurnKey, TurnQueue, TurnState


def add(queue, request_id, **kwargs):
    return queue.enqueue("session-a", request_id, 0, text_private="private text", **kwargs)


def test_fifo_dispatch_requires_explicit_success_gate():
    queue = TurnQueue()
    first, second = add(queue, "one"), add(queue, "two")
    assert queue.dispatch_next() == first
    assert queue.dispatch_next() is None
    assert queue.terminal_success(first)
    assert queue.dispatch_next() == second


def test_editing_head_blocks_dispatch_and_delete_never_hides_active():
    queue = TurnQueue()
    first, second = add(queue, "one"), add(queue, "two")
    assert queue.begin_edit(first, text_private="edited")
    assert queue.dispatch_next() is None
    assert queue.save_edit(first)
    assert queue.dispatch_next() == first
    assert not queue.delete(first)
    assert queue.terminal_success(first)
    assert queue.dispatch_next() == second


def test_edit_and_dispatch_are_atomic_under_race():
    for number in range(40):
        queue = TurnQueue()
        first, second = add(queue, f"first-{number}"), add(queue, f"second-{number}")
        barrier = threading.Barrier(3)
        outcomes = []

        def edit():
            barrier.wait()
            outcomes.append(("edit", queue.begin_edit(first)))

        def dispatch():
            barrier.wait()
            outcomes.append(("dispatch", queue.dispatch_next()))

        threads = [threading.Thread(target=edit), threading.Thread(target=dispatch)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        snapshot = queue.snapshot()
        assert snapshot.active is None or snapshot.active.key == first
        assert [turn.key for turn in snapshot.waiting] in ([first, second], [second])
        # A successful edit can only leave the FIFO head editing, never permit second to overtake.
        if dict(outcomes)["edit"]:
            assert snapshot.active is None
            assert snapshot.waiting[0].state is TurnState.EDITING


def test_delete_and_dispatch_are_atomic_under_race():
    for number in range(40):
        queue = TurnQueue()
        first = add(queue, f"first-{number}")
        barrier = threading.Barrier(3)
        outcomes = []

        def delete():
            barrier.wait()
            outcomes.append(queue.delete(first))

        def dispatch():
            barrier.wait()
            outcomes.append(queue.dispatch_next())

        threads = [threading.Thread(target=delete), threading.Thread(target=dispatch)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        snapshot = queue.snapshot()
        assert snapshot.active is None or snapshot.active.key == first
        assert not snapshot.waiting
        assert len([outcome for outcome in outcomes if outcome]) == 1


def test_interrupt_is_not_terminal_and_late_event_cannot_touch_next_turn():
    queue = TurnQueue()
    first, second = add(queue, "one"), add(queue, "two")
    assert queue.dispatch_next() == first
    assert queue.request_interrupt(first)
    assert queue.snapshot().active.state is TurnState.INTERRUPT_REQUESTED
    assert queue.confirmed_interrupt(first)
    assert queue.snapshot().held
    assert not queue.dispatch_next()
    assert queue.resume()
    assert queue.dispatch_next() == second
    assert not queue.terminal_success(first)
    assert queue.snapshot().active.key == second


def test_stop_success_race_holds_queue_and_terminal_releases_private_payload():
    queue = TurnQueue()
    first, second = add(queue, "one"), add(queue, "two")
    assert queue.dispatch_next() == first
    assert queue.request_interrupt(first)
    assert queue.terminal_success(first)  # provider success won the interrupt race
    assert queue.snapshot().held
    assert queue._turns[first].text_private == ""
    assert queue._turns[first].image_sizes == ()
    assert queue.resume()
    assert queue.dispatch_next() == second


def test_unknown_requires_terminal_reconciliation_then_explicit_resume():
    queue = TurnQueue()
    first, second = add(queue, "one"), add(queue, "two")
    assert queue.dispatch_next() == first
    assert queue.mark_unknown(first)
    assert not queue.resume()
    assert queue.terminal_success(first)
    assert queue.snapshot().held
    assert queue.resume()
    assert queue.dispatch_next() == second


def test_failure_holds_and_cross_generation_terminal_event_is_rejected():
    queue = TurnQueue()
    first = add(queue, "one")
    assert queue.dispatch_next() == first
    assert not queue.terminal_success(TurnKey("session-a", "one", 1))
    assert queue.terminal_failure(first)
    assert queue.snapshot().held
    assert queue.resume()


def test_request_ids_stay_unique_after_terminal_or_delete():
    queue = TurnQueue()
    first = add(queue, "same")
    assert queue.delete(first)
    with pytest.raises(ValueError, match="already"):
        add(queue, "same")
    second = add(queue, "other")
    assert queue.dispatch_next() == second
    assert queue.terminal_success(second)
    with pytest.raises(ValueError, match="already"):
        add(queue, "other")


def test_queue_binds_first_session_and_attempt_generation():
    queue = TurnQueue()
    add(queue, "one")
    with pytest.raises(ValueError, match="different session"):
        queue.enqueue("session-b", "two", 0)
    with pytest.raises(ValueError, match="different attempt"):
        queue.enqueue("session-a", "three", 1)


def test_waiting_limit_excludes_active_and_rejected_enqueue_does_not_burn_id():
    queue = TurnQueue()
    active = add(queue, "active")
    assert queue.dispatch_next() == active
    for number in range(5):
        add(queue, f"waiting-{number}")
    with pytest.raises(OverflowError):
        add(queue, "retryable")
    assert queue.delete(queue.snapshot().waiting[-1].key)
    retry = add(queue, "retryable")
    assert retry.request_id == "retryable"


def test_hold_does_not_cancel_active_work():
    queue = TurnQueue()
    first = add(queue, "one")
    assert queue.dispatch_next() == first
    queue.hold()
    assert queue.snapshot().active.key == first
    assert queue.terminal_success(first)
    assert queue.resume()


def test_image_limits_account_for_edit_copies_and_snapshot_is_redacted():
    queue = TurnQueue()
    key = add(queue, "images", image_sizes=[5 * MIB] * 4)
    assert queue.snapshot().queued_image_bytes == 20 * MIB
    assert queue.begin_edit(key)
    assert queue.snapshot().queued_image_bytes == 40 * MIB
    assert "private text" not in repr(queue)
    assert "private text" not in repr(queue.snapshot())
    assert queue.cancel_edit(key)
    with pytest.raises(ValueError):
        add(queue, "bad-bool", image_sizes=[True])
    with pytest.raises(ValueError):
        add(queue, "bad-count", image_sizes=[0] * 5)
    with pytest.raises(ValueError):
        add(queue, "bad-size", image_sizes=[5 * MIB + 1])
    with pytest.raises(ValueError):
        add(queue, "bad-negative", image_sizes=[-1])
    with pytest.raises(ValueError):
        add(queue, "bad-float", image_sizes=[1.0])
