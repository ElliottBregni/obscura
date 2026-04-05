fpath = "/Users/elliottbregni/dev/obscura-main/obscura/cli/__init__.py"
with open(fpath, "r", encoding="utf-8") as f:
    content = f.read()

KAIROS_MARKER = "# ---------------------------------------------------------------------------\n# kairos command group\n# ---------------------------------------------------------------------------"
CHANNELS_MARKER = "# ---------------------------------------------------------------------------\n# channels command group\n# ---------------------------------------------------------------------------"

# Count kairos blocks
count = content.count(KAIROS_MARKER)
print(f"Found {count} kairos blocks")

if count <= 1:
    print("Nothing to fix.")
    exit(0)

# Strategy: find the position of the channels marker,
# then find the LAST kairos marker before channels,
# keep everything from that last kairos marker onward,
# replace everything from the first kairos marker to last kairos marker with nothing.

first_kairos = content.index(KAIROS_MARKER)
channels_pos = content.index(CHANNELS_MARKER)

# Find last kairos marker before channels
last_kairos = content.rindex(KAIROS_MARKER, 0, channels_pos)

print(f"First kairos at: {first_kairos}")
print(f"Last kairos at: {last_kairos}")
print(f"Channels at: {channels_pos}")

# Remove everything between first_kairos and last_kairos (the duplicates)
new_content = content[:first_kairos] + content[last_kairos:]

remaining = new_content.count(KAIROS_MARKER)
print(f"Remaining kairos blocks after fix: {remaining}")

if remaining == 1:
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(new_content)
    print(
        f"SUCCESS. File now {len(new_content)} bytes ({new_content.count(chr(10))} lines)"
    )
else:
    print(f"ERROR: expected 1 block remaining, got {remaining}. Not writing.")
