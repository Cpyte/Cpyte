import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'source'))
from cpyte.mainpie import main
sys.exit(main())
