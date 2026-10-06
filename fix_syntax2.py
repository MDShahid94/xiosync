with open("colab/xiorun_agent.py", "r") as f:
    lines = f.readlines()

out = []
skip = False
for line in lines:
    if line.strip() == ".replace(\"{TIMEZONE}\",    _uc_timezone)":
        if out and out[-1].strip() == ")":
            skip = True
    
    if skip:
        if line.strip() == ".replace(\"{TZ_OFFSET}\",   str(_tz_off2))":
            skip = False
            continue
        elif line.strip() == ")":
            pass
    if not skip:
        out.append(line)

with open("colab/xiorun_agent.py", "w") as f:
    f.writelines(out)
