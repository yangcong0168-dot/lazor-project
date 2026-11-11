"""Compile exact dimension-only masks for batched optimistic ray traversal.

Each odd-parity face midpoint occupies four bit positions, one per direction.
The returned incidence masks include one final entry for the outside cell.
Routes are (signed shift, source-state mask) pairs. Masks exclude transitions
that leave the board, so applying a route cannot wrap across its boundary.

Incidence integers can consume quadratic space in the state count. Keep the
hard limit even when a caller also bounds its cache by retained state slots.
This module compiles immutable values; cache ownership belongs to the caller.
"""

MAX_STATES = 20000


def compile_bitboards(width: int, height: int, checkpoint):
    """Return (front_masks, rear_masks, straight_routes, reflected_routes).

    A cell's top face starts at 4*(row*(2*width+1)+column). Its left,
    right and bottom faces have fixed offsets. Shifting this eight-bit
    template constructs all incidences without iterating every ray state.
    ``checkpoint`` is called before allocation, once per row and at the end;
    its exceptions propagate, and no partially built table is published.
    """
    if (type(width) is not int or type(height) is not int
            or width < 1 or height < 1):
        raise ValueError("Bitboard dimensions must be positive integers.")
    stride = 2 * width + 1
    count = 4 * (stride * (2 * height + 1) // 2)
    if count > MAX_STATES:
        raise ValueError(f"Bitboard compilation is limited to {MAX_STATES} states.")
    checkpoint()
    full = (1 << count) - 1
    front_template = (10 | (12 << (4 * width)) | (3 << (4 * (width + 1)))
                      | (5 << (4 * stride)))
    rear_template = (5 | (3 << (4 * width)) | (12 << (4 * (width + 1)))
                     | (10 << (4 * stride)))
    front_masks, rear_masks = [], []
    vertical = 0
    vertical_row = (1 << (4 * (width + 1))) - 1
    for row in range(height):
        checkpoint()
        for column in range(width):
            shift = 4 * (row * stride + column)
            front_masks.append(front_template << shift)
            rear_masks.append(rear_template << shift)
        vertical |= vertical_row << (4 * (row * stride + width))
    # Every state has exactly one front and one rear: the cell masks are
    # disjoint, making addition equivalent to OR for each partition.
    front_outside = full ^ sum(front_masks)
    rear_outside = full ^ sum(rear_masks)
    front_masks.append(front_outside)
    rear_masks.append(rear_outside)
    horizontal = full ^ vertical
    direction_zero = full // 15  # One low bit in every four-bit state group.
    straight_groups, reflected_groups = {}, {}
    directions = ((-1, -1), (-1, 1), (1, -1), (1, 1))
    for direction, (vx, vy) in enumerate(directions):
        direction_mask = direction_zero << direction
        straight_groups[2 * (vy * stride + vx)] = direction_mask & ~front_outside
        for face, offset in ((vertical, 2 * vy * stride - 4 * vx),
                             (horizontal, -2 * vy * stride + 2 * vx - vy)):
            mask = direction_mask & face & ~rear_outside
            reflected_groups[offset] = reflected_groups.get(offset, 0) | mask
    checkpoint()
    return (tuple(front_masks), tuple(rear_masks), tuple(straight_groups.items()),
            tuple(reflected_groups.items()))
