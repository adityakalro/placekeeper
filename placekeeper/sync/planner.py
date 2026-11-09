"""Pure source selection and destination planning."""
from placekeeper.errors import UnsupportedMapping


def receipt_matches(observation, receipt):
    return (receipt and receipt['position'] == observation.native
            and receipt['stamp'] == observation.stamp)


def choose_source(observations, receipts):
    # A rounded position from our own write must not trigger another handoff.
    furthest = max(position.anchor for position in observations.values())
    return next((key for key, position in observations.items()
                 if position.anchor == furthest and not position.own
                 and not receipt_matches(position, receipts.get(key))), None)


def plan(readers, observations, receipts, handoff=None, skipped=None):
    if len(observations) < 2:
        return None, {}, 'Fewer than two readers have saved positions for this book.'
    source = choose_source(observations, receipts)
    if source is None and handoff:
        key = handoff['source']
        current = observations.get(key)
        if (current is not None and not current.own
                and current.anchor == handoff['anchor']
                and receipt_matches(current, handoff['position'])
                and all(position.anchor <= current.anchor or position.own
                        or receipt_matches(position, receipts.get(other))
                        for other, position in observations.items())):
            source = key
    if source is None:
        return None, {}, 'No new reader position.'
    anchor = observations[source].anchor
    targets = {}
    for key, current in observations.items():
        if current.anchor >= anchor:
            continue
        if readers[source].equivalent(anchor, current.anchor):
            continue
        try:
            target = readers[key].target(anchor, current)
        except UnsupportedMapping as error:
            if skipped is not None:
                skipped[key] = str(error)
            continue
        if target is not None and target != current.native:
            targets[key] = target
    return source, targets, ''
