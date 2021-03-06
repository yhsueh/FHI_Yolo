import os
import glob
import sys

from matplotlib import pyplot as plt
from PIL import Image
import cv2
import numpy as np

import yolo
import fhi_unet as unet
import fhi_lib.distance_estimator as de
import fhi_lib.img_coordinate as ic
import fhi_lib.geometry as ge

def yolo_detection(user_input):
	img_dir = os.path.join(os.getcwd(), user_input['image_dir'])
	yl_weight = os.path.join(os.getcwd(), user_input['yolo_weight'])
	yl_output_dir = os.path.join(os.getcwd(), user_input['yolo_output_dir'])
	
	yl = yolo.YOLO(model_path=yl_weight)
	yl_results = []
	
	for img_path in glob.glob(img_dir + r'\*.jpg'):
		print('Processing:', img_path)
		img = Image.open(img_path)
		result = yl.detect_image(img, True)

		# save yolo image result
		basename = os.path.basename(img_path)
		yl_save_path = os.path.join(yl_output_dir, basename)
		cv2.imwrite(yl_save_path, cv2.cvtColor(result['result_img'], cv2.COLOR_BGR2RGB))

		# add image path to yolo result dictionary
		result.update({'img_path' : img_path})
		yl_results.append(result)
		break
	return yl_results

def create_masks(un, yl_results, un_output_dir):
	def enlarge_roi(roi):
		center = ((roi[0] + roi[2])/2, (roi[1] + roi[3])/2)
		width = 1.4*(roi[2] - roi[0])
		height = 1.4*(roi[3] - roi[1])
		enlarged_roi = (int(center[0]-0.5*width), int(center[1]-0.5*height),
				int(center[0]+0.5*width), int(center[1]+0.5*height))
		return enlarged_roi

	def unet_crop(yl_result):
		img = yl_result['original_img']
		cropped_imgs = []
		masks = []
		mask_coords = []

		for i, roi in enumerate(yl_result['rois']):
			# Enlarge the roi boundry acquired from Yolo
			roi = enlarge_roi(roi)
			
			# Cropped the image
			cropped_img = img[roi[1]:roi[3], roi[0]:roi[2],:]
			mask_coord = (roi[0], roi[1])

			# UNet Detection
			mask = un.detect(cropped_img)

			# Save masks
			basename = os.path.basename(yl_result['img_path'])
			filename = os.path.splitext(basename)[0]
			mask_save_path = os.path.join(un_output_dir, basename)
			mask_save_path = mask_save_path.replace(filename, filename + r'_{}'.format(i))
			cv2.imwrite(mask_save_path, mask)

			# Image Processing
			morphology_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3,3))
			dilation = cv2.dilate(mask, morphology_kernel, iterations=3)
			mask = cv2.erode(dilation, morphology_kernel, iterations=3)
			_, mask = cv2.threshold(mask, 255/2, 255, cv2.THRESH_BINARY)

			# Save results
			cropped_imgs.append(cropped_img)
			masks.append(mask)
			mask_coords.append(mask_coord)

		yl_result.update({'cropped_imgs' : cropped_imgs,
							'masks' : masks,
							'mask_coords' : mask_coords})

	for yl_result in yl_results:
		unet_crop(yl_result)

def unet_detection(user_input, yl_results):
	un_output_dir = user_input['unet_output_dir']
	un_weight_dir = user_input['unet_weight_dir']
	result_dir = user_input['result_dir']
	
	# Start unet detection
	print('#### unet initialization completed ####')
	un = unet.UNET(un_weight_dir)
	un.initialize()
	create_masks(un, yl_results, un_output_dir)
	un.close_session()
	
	print('#### Begin computing real-world distance ####')
	for yl_result in yl_results:
		compute_distance(result_dir, yl_result)

def compute_distance(result_dir, yl_result):
	def resize_restoration(mask_itr_pt, cropped_shape):
		unet_resize = 128
		aspect_ratio = cropped_shape[1]/cropped_shape[0] #x/y
		itr_pt = mask_itr_pt.get_point_tuple()

		restored_x = 0
		restored_y = 0

		if aspect_ratio >=1:
			distorted_y = unet_resize / aspect_ratio
			padding_y = (unet_resize - distorted_y)/2

			restored_x = itr_pt[0] * cropped_shape[1] / unet_resize
			restored_y = (itr_pt[1] - padding_y) * cropped_shape[1] / unet_resize
		else:
			distorted_x = unet_resize / aspect_ratio
			padding_x = (unet_resize - distorted_x)/2

			restored_x = (itr_pt[0] - padding_x) * cropped_shape[0] / unet_resize
			restored_y = itr_pt[1] * cropped_shape[0] / unet_resize
		return ge.Point((int(restored_x), int(restored_y)))

	img = yl_result['original_img']
	estimator = de.DistanceEstimator(img)
	estimator.initialize()
	img = estimator.display_reference_pts(img)
	cropped_imgs = yl_result['cropped_imgs']
	masks = yl_result['masks']
	mask_coords = yl_result['mask_coords']
	_, ax = plt.subplots()

	for i, mask in enumerate(masks):
		roi = yl_result['rois'][i]
		class_id = yl_result['class_ids'][i]		
		info = (mask, roi, class_id)
		mask_coord = mask_coords[i]
		cropped_img = cropped_imgs[i]
		pt_itr = None

		if class_id == 0 or class_id == 1:
			accessory = ic.Type1_2Coord(info)
			pt_itr = accessory.get_point_of_interest()
			pt_itr = resize_restoration(pt_itr, cropped_img.shape).add_point(mask_coord)			
			accessory.update_interest_pt(pt_itr.get_point_tuple())
			img = accessory.draw_point_of_interest(img)
		else:
			accessory = ic.Type3_4Coord(info)
			pt_itr = accessory.get_point_of_interest()
			pt_itr = resize_restoration(pt_itr, cropped_img.shape).add_point(mask_coord)			
			accessory.update_interest_pt(pt_itr.get_point_tuple())
			img = accessory.draw_point_of_interest(img)

		# Distance estimator
		caption = estimator.estimate(pt_itr)
		ax.text(roi[0], roi[1], caption, color='lime', weight='bold', size=6, backgroundcolor="none")

	print('Process completed')
	img_path = yl_result['img_path']
	save_path = os.path.join(result_dir, os.path.basename(img_path))
	ax.text(img.shape[1]/2-600, img.shape[0]-40, os.path.splitext(os.path.basename(img_path))[0],
	 color='white', weight='bold', size=6, va='center', backgroundcolor='none')
	img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
	plt.imshow(img)
	plt.savefig(save_path, dpi=300)