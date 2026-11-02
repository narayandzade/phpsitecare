import sys
import engine

job = engine.load(sys.argv[1])
engine.run(job)
