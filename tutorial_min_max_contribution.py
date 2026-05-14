# DS614 Open Source Contribution
# File: python/pyarrow/compute.py (excerpt)
#
# This function was added to the Apache Arrow source code
# at: python/pyarrow/compute.py
# Branch: GH-DS614-tutorial-min-max
# Commit: GH-DS614: [Python] Add tutorial_min_max compute function
#         following official Arrow contribution guide
#
# Reference: https://arrow.apache.org/docs/dev/developers/guide/tutorials/python_tutorial.html

import pyarrow as pa
from pyarrow.compute import ScalarAggregateOptions, call_function


def tutorial_min_max(values, skip_nulls=True):
    """
    Compute the min-1 and max+1 of a numeric array.

    This is a tutorial function added following the official Apache Arrow
    Python contribution tutorial. It extends pc.min_max by returning
    values one step outside the observed range.

    Parameters
    ----------
    values : array-like
        Input numeric values.
    skip_nulls : bool, default True
        Whether to skip null values when computing min/max.

    Returns
    -------
    pa.Scalar
        A struct scalar with fields 'min-' (min value - 1)
        and 'max+' (max value + 1).

    Examples
    --------
    >>> import pyarrow as pa
    >>> tutorial_min_max([4, 5, 6, None, 1])
    # Returns: {min-: 0, max+: 7}
    # Compare: pc.min_max([4,5,6,None,1]) -> {min: 1, max: 6}
    """
    options = ScalarAggregateOptions(skip_nulls=skip_nulls)
    arr = pa.array(values) if not hasattr(values, 'type') else values

    min_max = call_function("min_max", [arr], options)

    if min_max[0].as_py() is None:
        min_t = min_max[0].as_py()
        max_t = min_max[1].as_py()
    else:
        min_t = min_max[0].as_py() - 1
        max_t = min_max[1].as_py() + 1

    ty = pa.struct([pa.field("min-", pa.int64()), pa.field("max+", pa.int64())])
    return pa.scalar([("min-", min_t), ("max+", max_t)], type=ty)


# Verification — run this to confirm it works
if __name__ == "__main__":
    import pyarrow.compute as pc

    data = [4, 5, 6, None, 1]

    original = pc.min_max(data)
    modified = tutorial_min_max(data)

    print("Original pc.min_max :", original)
    print("tutorial_min_max    :", modified)
    print()
    print("Branch  : GH-DS614-tutorial-min-max")
    print("Commit  : GH-DS614: [Python] Add tutorial_min_max compute function")
    print("File    : python/pyarrow/compute.py")
