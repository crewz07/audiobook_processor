#!/bin/bash
# The 9 books matched by hand on 2026-11-24. Pass --dry-run to only show the plan.
PY="/c/Audiobook_Work/.venv/Scripts/python.exe"; A="/c/Users/kruse/OneDrive/Music/Audiobooks"; MR="$A/Vince Flynn - Mitch Rapp 01 - 14 The Survivor"
export PYTHONIOENCODING=utf-8
run() { echo; echo "################ $1"; shift; "$PY" audiobook.py "$@" --yes $EXTRA; }
EXTRA="$*"
run "Threshing Day"       "$A/Rebecca Yarros - Threshing Day The Empyrean GA.m4b" --asin B0H9MGBCFZ
run "The Last Man"        "$MR/13. Vince Flynn and Kyle Mills - Mitch Rapp 13 - The Last Man.m4b" --asin B009PPNDWS
run "Inimitable Jeeves"   "$A/P. G. Wodehouse/The Inimitable Jeeves" --asin B002UZN4OM
run "Brisingr"            "$A/The Inheritance Cycle/03 Brisingr 2008" --asin B002V0A444
run "The Third Option"    "$MR/4. Vince Flynn - Mitch Rapp 04 - The Third Option.m4b" --asin B004YLIJH2
run "Transfer of Power"   "$MR/3. Vince Flynn - Mitch Rapp 03 - Transfer Of Power.m4b" --asin B004YLIJ04 --chapters files
run "Manufactured Witches" "$A/mm-ya--manufactured_witches-michelle_rene" --asin B0DCXKMDDW
run "Conan the Conqueror" "$A/Conan the Barbarian Cimmerian Series/Howard Robert E and Sprague de Camp Lin Carter - Conan Series/08 Conan the Conqueror" \
    --title "Conan the Conqueror" --author "Robert E. Howard" --year 1950 --series "Conan" --part 8 --chapters files
run "Necropolis Railway"  "$A/The Necropolis Railway" \
    --title "The Necropolis Railway" --author "Andrew Martin" --year 2002 --series "Jim Stringer" --part 1 --chapters files
