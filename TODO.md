# To do

## Resting HR is only comparable between days with the same overnight wear

Found while testing: Claude reported resting HR of 41–42 bpm against a 4-week average of
56 as "a big improvement". The low days were exactly the days with sleep recorded. Garmin
takes resting HR from the lowest sustained HR of the day, usually during sleep; without
the watch overnight it comes from daytime and reads 5–15 bpm higher. The `wear_s >= 4h`
filter doesn't catch this.

- [ ] **Expose it in the tools.** Add a `slept_with_watch` flag (sleep recorded that
  night) to each day in `daily_metrics`, and say in its description that resting HR is
  only comparable between days with the same value. In `compare_periods`, report resting
  HR split by nights with and without the watch, or warn when a period mixes them.
- [ ] **Document it** as a caveat in `docs/schema.md`, next to the partial-wear one
  (daily table, "Things the code must handle").
- [ ] **Add it as an eval case:** "Has my resting heart rate improved?" Correct: it can't
  be told from mixed days, or compare only nights with the watch on. Tests whether the
  tools stop Claude from overclaiming.
