# Count representation contract

Raw UMI counts are the input expected by count models and by methods that perform their own
normalization. They are normally sparse, finite, nonnegative integers. An integer matrix can still
be biologically inappropriate (for example already filtered features or transformed values that
were rounded), so the report records where it came from rather than claiming more than it checks.

For H5AD inputs:

- `X` is often raw counts in newly created objects, but may be normalized or scaled.
- `layers["counts"]` is a common explicit raw-count location.
- `.raw.X` is usable only when it covers every current gene and can be aligned to `var_names`.
- multiple valid alternatives are not interchangeable; `auto` refuses ambiguity.

The tool preserves the original object metadata and other layers, writes the selected matrix to
`layers["counts"]`, and also places it in `X` so the output is unambiguously count-ready. It never
overwrites the input.

Gene identifiers are independent of count validity. Ensembl IDs such as human
`ENSG00000141510` or mouse `ENSMUSG00000059552` are valid feature identifiers for a count matrix;
reference models may separately require symbols such as human `TP53` or mouse `Trp53`.
