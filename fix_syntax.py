with open("colab/xiorun_agent.py") as f:
    lines = f.readlines()

out = []
skip = False
for line in lines:
    if line.strip() == '.replace("{TIMEZONE}",    _timezone)':
        if out and out[-1].strip() == ")":
            # We hit the stray block
            skip = True

    if skip:
        if line.strip() == '.replace("{TZ_OFFSET}",   str(_tz_offset))':
            skip = False
            continue
        elif line.strip() == ")":
            # wait, the stray block ends with `)`
            pass
    if not skip:
        out.append(line)

with open("colab/xiorun_agent.py", "w") as f:
    f.writelines(out)
