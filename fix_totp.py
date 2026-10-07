import re

with open("colab/xiorun_agent.py") as f:
    code = f.read()

replacement = """                                logger.info(f"uc-login: left challenge → {driver.current_url[:70]}")
                                break
                        except Exception:
                            break
                    time.sleep(1.5)
                else:
                    logger.warning("uc-login: CDP TOTP fill failed — could not find input")
            except Exception as _te:
                logger.warning(f"uc-login: TOTP block failed: {_te}")
        elif not _is_challenge:"""

code = re.sub(
    r"                                logger\.info\(f\"uc-login: left challenge → \{driver\.current_url\[:70\]\}\"\)\n                                break\n                        except Exception:\n                            break\n                    time\.sleep\(1\.5\)\n                else:\n                    logger\.warning\(\"uc-login: CDP TOTP fill failed — could not find input\"\)\n                    logger\.info\(\"uc-login: TOTP submitted via CDP\"\)\n                else:\n                    # send_keys fallback with XIOBR robust clear \(4 strategies\)(.*?)logger\.warning\(f\"uc-login: TOTP block failed: \{_te\}\"\)\n        elif not _is_challenge:",
    replacement,
    code,
    flags=re.DOTALL,
)

with open("colab/xiorun_agent.py", "w") as f:
    f.write(code)
