# Goal
profile: software

Show a catalog item detail: clicking Save persists the title, an empty
title shows a visible error, and after a failed save the form stays
editable.

## Definition of done
- Clicking Save with title "Oak desk" shows "Saved" and the title field
  still reads "Oak desk".
- Clicking Save with an empty title shows error text "Title is required"
  and does not leave the form in a success state.
- After that error, the user can type again without a full page reload.

## Constraints
- Synthetic catalog UI. No live store install.
