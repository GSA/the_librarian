import os, sys, runpy
sys.path.insert(0, os.getcwd())
exec(open(".local/plans/proto/p4_events_cache.py").read().split("sys.argv =")[0])
sys.argv = ["e8"] + sys.argv[1:]
runpy.run_path(".local/plans/proto/e8_concurrency.py", run_name="__main__")
