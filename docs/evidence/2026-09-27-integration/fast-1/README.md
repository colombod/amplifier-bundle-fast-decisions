# Duration parser repair
Repair `solution.py` using only the Python standard library. Keep the public
function `parse_duration(s)`. Run `python3 -m unittest -v test_public.py`.

Input is a string composed of optional components in this exact order: an
integer followed by "h", then an integer followed by "m", then an integer
followed by "s" (each component optional, but at least one must be present).
No other characters, spaces, or signs are allowed. Integers are written in
decimal without a leading "+"; leading zeros are fine (e.g. "05m").

Return the total number of seconds as a nonnegative int. Reject any other
string (wrong order, unknown unit, missing digits, extra characters, empty
string) with ValueError. Reject a non-string input with TypeError.
