all:
	python main.py --device 0 --load-weights pretrained_models/dev_19.40_PHOENIX14-T.pt --phase test --batch-size 1 --test-batch-size 1

cpu:
	python main.py --device None --load-weights pretrained_models/dev_19.40_PHOENIX14-T.pt --phase test --batch-size 1 --test-batch-size 1