import cv2
import numpy as np
import multiprocessing
import time
from sklearn.externals import joblib
from joblib import Parallel
from joblib import delayed
from scipy.ndimage.measurements import label
from image_features import *
 
# Bounding Box related configs
dist_thresh = 0.1  # Threshold for relative distance to join objects
last_seen_thresh = 24  # Numbers of frames to wait before deleting a previously detected object 
           # without any new objects on the heatmap

# The algorithm tries to run each search area on a separate cpu core
n_parallel_jobs = 8

MODEL_FOLDER = 'model'  # artifacts folder

# Use the same parameters from model learning, while searching sliding windows. 
color_space = 'YUV'  # Can be RGB, HSV, LUV, HLS, YUV, YCrCb
orient = 8  # HOG orientations
pix_per_cell = 4  # HOG pixels per cell
cell_per_block = 2  # HOG cells per block
hog_channel = 0  # Can be 0, 1, 2, or "ALL"
spatial_size = (32, 32)  # Spatial binning dimensions
hist_bins = 32  # Number of histogram bins
spatial_feat = True  # Spatial features on or off
hist_feat = True  # Histogram features on or off
hog_feat = True  # HOG features on or off



def get_center_points(boxes):
    """
    calculates center points of each box
    """
    x1 = boxes[:, 0]
    y1 = boxes[:, 1]
    x2 = boxes[:, 2]
    y2 = boxes[:, 3]
    width = x2 - x1
    height = y2 - y1
    x = x1 + width // 2
    y = y1 + height // 2
    return np.stack((x, y)).T

def get_closest_box(box, o_boxes):
    """
    finds the box which is closest
    calculates the average diagonal between the given box and other boxes and
        puts it in relation to the distance of the center points.

    returns the distance and box index which is closest
    """

    box = box.astype(np.float)
    o_boxes = o_boxes.astype(np.float)
    mean_diag = (np.sqrt(box[0] ** 2 + box[2] ** 2) + 
                 np.sqrt(o_boxes[:, 0] ** 2 + o_boxes[:, 2] ** 2)) / 2
    c_box = get_center_points(np.expand_dims(box, axis=0))[0]
    c_o_boxes = get_center_points(o_boxes)
    dist_centers = np.sqrt(np.power(c_box[0] - c_o_boxes[:, 0], 2) + 
            np.power(c_box[1] - c_o_boxes[:, 1], 2))

    distances = dist_centers / mean_diag
    
    closest_dist = distances.min()       # minimum distance
    closest_box = distances.argmin()    # index of the box with minimum distance
    return closest_dist, closest_box



# all detected objects are instances of this class
class BoxObject:
    def __init__(self, box):
        """
        Describes a BoxObject that was detected on a image.
        :param box: initial bounding box
        """
        self.is_hidden = False
        self.last_boxes = []
        self.avg_box = None
        self.last_seen = 0
        self.age = 0
        self.num_frames = 10

        self.update(box)

    def update(self, box):
        """
        Updates the BoxObject with a new bounding box.
        """
        if box is not None:
            self.last_boxes.append(box)
            bound = min(len(self.last_boxes), self.num_frames)
            self.avg_box = np.mean(self.last_boxes[-bound:], axis=0).astype(np.uint32)
            self.last_seen = 0
        else:
            self.last_seen += 1

        self.age += 1

    def unhide(self, box):
        """
        Unhides the BoxObject and sets the bounding box to the no value
        """
        if self.is_hidden:
            self.last_boxes.extend([box] * self.num_frames)
            self.is_hidden = False

    def draw(self, img, color=(0, 0, 255), thick=3):
        """
        Draws the bounding box of the BoxObject on a given image.
        """
        if self.avg_box is None:
            return img

        box_to_draw = np.zeros(4, dtype=np.uint32)
        if self.is_hidden:
            w = self.avg_box[2] - self.avg_box[0]
            h = self.avg_box[3] - self.avg_box[1]

            box_to_draw[:2] = self.avg_box[:2] + min(25, w // 2)
            box_to_draw[2:] = self.avg_box[2:] - min(25, h // 2)
        else:
            box_to_draw = self.avg_box

        cv2.rectangle(img, (box_to_draw[0], box_to_draw[1]), (box_to_draw[2], box_to_draw[3]), color, thick)
        return img


# processes the image to detect objects
class ObjectProcess:
    
    def __init__(self):
        
        self.objects = []
        
        # load svm classifiers
        self.model = joblib.load(MODEL_FOLDER + '/svc.pkl') 
        self.pca = joblib.load(MODEL_FOLDER + '/pca.pkl') 
        self.X_scaler = joblib.load(MODEL_FOLDER + '/x_scaler.pkl') 
    
    # takes an image, start and stop positions in both x and y, window size (x and y dimensions),  
    # and overlap fraction (for both x and y)
    def slide_window(self, img, x_start_stop=[None, None], y_start_stop=[None, None],
                        xy_window=(64, 64), xy_overlap=(0.5, 0.5)):
        
        # If x and/or y start/stop positions not defined, set to image size
        if x_start_stop[0] == None:
            x_start_stop[0] = 0 
        if x_start_stop[1] == None:
            x_start_stop[1] = img.shape[1]
        if y_start_stop[0] == None:
            y_start_stop[0] = 0
        if y_start_stop[1] == None:
            y_start_stop[1] = img.shape[0]
        # Compute the span of the region to be searched    
        xspan = x_start_stop[1] - x_start_stop[0]
        yspan = y_start_stop[1] - y_start_stop[0]
        # Compute the number of pixels per step in x/y
        nx_pix_per_step = np.int(xy_window[0] * (1 - xy_overlap[0]))
        ny_pix_per_step = np.int(xy_window[1] * (1 - xy_overlap[1]))
        # Compute the number of windows in x/y
        nx_windows = np.int(xspan / nx_pix_per_step) - 1
        ny_windows = np.int(yspan / ny_pix_per_step) - 1
        
    # Initialize a list to append window positions to
        window_list = []
        
    # Loop through finding x and y window positions
        for ys in range(ny_windows):
            for xs in range(nx_windows):
                # Calculate window position
                startx = xs * nx_pix_per_step + x_start_stop[0]
                endx = startx + xy_window[0]
                starty = ys * ny_pix_per_step + y_start_stop[0]
                endy = starty + xy_window[1]
                
                # Append window position to list
                window_list.append(((startx, starty), (endx, endy)))
        # Return the list of windows
        return window_list
    
    # draws bounding boxes
    def draw_boxes(self, img, bboxes, color=(0, 0, 255), thick=6):
        # Make a copy of the image
        imcopy = np.copy(img)
        # Iterate through the bounding boxes
        for bbox in bboxes:
            # Draw a rectangle given bbox coordinates
            cv2.rectangle(imcopy, bbox[0], bbox[1], color, thick)
        # Return the image copy with boxes drawn
        return imcopy
    
    # search for objects given an image and the list of windows to be searched (output of slide_windows())
    def search_windows(self, img, windows, clf, scaler, pca, color_space='RGB',
                        spatial_size=(32, 32), hist_bins=32,
                        hist_range=(0, 256), orient=9,
                        pix_per_cell=8, cell_per_block=2,
                        hog_channel=0, spatial_feat=True,
                        hist_feat=True, hog_feat=True):
    
        # 1) Create an empty list to receive positive object windows
        on_windows = []
        # 2) Iterate over all windows in the list
        for window in windows:
            # 3) Extract the test window from original image
            test_img = cv2.resize(img[window[0][1]:window[1][1], window[0][0]:window[1][0]], (64, 64))      
            # 4) Extract features for that window using single_img_features()
            features = extract_features_image(test_img, color_space=color_space,
                                spatial_size=spatial_size, hist_bins=hist_bins,
                                orient=orient, pix_per_cell=pix_per_cell,
                                cell_per_block=cell_per_block,
                                hog_channel=hog_channel, spatial_feat=spatial_feat,
                                hist_feat=hist_feat, hog_feat=hog_feat)
            # 5) Scale extracted features to be fed to classifier
            test_features = scaler.transform(np.concatenate(features).reshape(1, -1))
            # 6) Predict using your classifier
            test_features = pca.transform(test_features)
            prediction = clf.predict(test_features)
            # 7) If positive (prediction == 1) then save the window
            if prediction == 1:
                on_windows.append(window)
        # 8) Return windows for positive objects
        return on_windows
    
    def add_heat(self, heatmap, bbox_list):
        # Iterate through list of bboxes
        for box in bbox_list:
            # Add += 1 for all pixels inside each bbox
            # Assuming each "box" takes the form ((x1, y1), (x2, y2))
            heatmap[box[0][1]:box[1][1], box[0][0]:box[1][0]] += 1
    
        # Return updated heatmap
        return heatmap  # Iterate through list of bboxes
        
    def apply_threshold(self, heatmap, threshold):
        # Zero out pixels below the threshold
        heatmap[heatmap <= threshold] = 0
        # Return thresholded map
        return heatmap
    
    def get_bboxes_from_labels(self, labels):
        
        boxes = []
        
        # Iterate through all detected cars
        for car_number in range(1, labels[1] + 1):
            # Find pixels with each car_number label value
            nonzero = (labels[0] == car_number).nonzero()
            # Identify x and y values of those pixels
            nonzeroy = np.array(nonzero[0])
            nonzerox = np.array(nonzero[1])
            # Define a bounding box based on min/max x and y
            bbox = ((np.min(nonzerox), np.min(nonzeroy)), (np.max(nonzerox), np.max(nonzeroy)))
            # Draw the box on the image
            x1 = bbox[0][0]
            y1 = bbox[0][1]
            x2 = bbox[1][0]
            y2 = bbox[1][1]
            
            boxes.append([x1, y1, x2, y2])
        # Return the image
        return boxes
    
    def draw_labeled_bboxes(self, img, labels):
        # Iterate through all detected cars
        for car_number in range(1, labels[1] + 1):
            # Find pixels with each car_number label value
            nonzero = (labels[0] == car_number).nonzero()
            # Identify x and y values of those pixels
            nonzeroy = np.array(nonzero[0])
            nonzerox = np.array(nonzero[1])
            # Define a bounding box based on min/max x and y
            bbox = ((np.min(nonzerox), np.min(nonzeroy)), (np.max(nonzerox), np.max(nonzeroy)))
            # Draw the box on the image
            cv2.rectangle(img, bbox[0], bbox[1], (255, 0, 255), 6)
        # Return the image
        return img
    
    def chunks(self, l, n):
        # Yield successive n-sized chunks from l
        for i in range(0, len(l), n):
            yield l[i:i + n]


    # Draws the bounding boxes for all objects which are old enough
    # age_threshold: minimum age of a object to be drawn
    def draw_info(self, img, age_threshold=8):
        n_vehicles = 0
        for object in self.objects:
            if len(object.last_boxes) > age_threshold:
                n_vehicles += 1
                img = object.draw(img, thick=2, color=(255, 50, 0))
    
        cv2.putText(img, 'Vehicles detected: %s' % n_vehicles, (50, 150), 
            cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 0), 3)
    
        return img
    
    # Removes bounding boxes which are to small
    def remove_outliers(self, boxes):
    
        filtered_boxes = []
        for bc in boxes:
            w = bc[2] - bc[0]
            h = bc[3] - bc[1]
            if bc[1] < 450 and w > 32 and h > 32:
                filtered_boxes.append(bc)
            elif bc[1] > 450 and w > 64 and h > 64:
                filtered_boxes.append(bc)
    
        return np.array(filtered_boxes)
    
    # Updates objects with new bounding boxes.
    def update_objects(self, boxes_contours):

        used_boxes = np.zeros(len(boxes_contours), np.bool)
        if boxes_contours is None or len(boxes_contours) == 0:
            for object in self.objects:
                object.update(None)
            return used_boxes
    
        for object in self.objects:
            closest_dist, closest_box = get_closest_box(object.avg_box, boxes_contours)
            if closest_dist < dist_thresh:
                if used_boxes[closest_box]:
                    object.is_hidden = True
    
                object.update(boxes_contours[closest_box])
                used_boxes[closest_box] = True
            else:
                object.update(None)
    
        return used_boxes
    
    # Unhides objects which have a bounding box close to them.
    def unhide_if_applicable(self, boxes_contours, used_boxes):

        unused_boxes = boxes_contours[used_boxes == False]
        if len(unused_boxes) > 0:
            hidden = [object for object in self.objects if object.is_hidden]
            for object in hidden:
                closest_dist, closest_box = get_closest_box( object.avg_box, unused_boxes)
                ix = np.where(np.all(boxes_contours == unused_boxes[closest_box], axis=1))[0][0]
                if closest_dist < 1.5 * dist_thresh:
                    object.unhide(boxes_contours[ix])
                    used_boxes[ix] = True
    
    # Creates a new object for every unused bounding box
    def create_new_objects(self, boxes_contours, used_boxes):
        for bb in boxes_contours[used_boxes == False]:
            d = BoxObject(bb)
            self.objects.append(d)
    
    # Removes all objects which havent been updated for a while or have been hidden to quickly
    def remove_lost_objects(self, age_threshold=8):

        keep_objects = []
        for object in self.objects:
            if object.last_seen < last_seen_thresh and \
                    not (object.is_hidden and object.age < age_threshold):
                keep_objects.append(object)
        self.objects = keep_objects


          
    def process_frame(self, image,
                    show_sliding_windows=False,
                    show_objects=False,
                    show_heatmap=False,
                    show_contours=False):
        
        draw_image = np.copy(image)
    
        # Uncomment the following line if you extracted training
        # data from .png images (scaled 0 to 1 by mpimg) and the
        # image you are searching is a .jpg (scaled 0 to 255)
        image = image.astype(np.float32) / 255
    
        y_start_stops = [ 
                         [400, 483],
                         [483, 650],
                        ]
    
        # Stride to use for each search area
        xy_windows = [
            (64, 64),
            (96, 96)
        ]
        
        windows = []
        
        for i, _ in enumerate(xy_windows):
            y_start_stop = y_start_stops[i]
            xy_window = xy_windows[i]
            _windows = self.slide_window(image, x_start_stop=[None, None], y_start_stop=y_start_stop,
                                    xy_window=xy_window, xy_overlap=(0.75, 0.75))
            windows.extend(_windows)
            
        # show sliding windows
        if show_sliding_windows:
            window_img = self.draw_boxes(draw_image, windows, color=(0, 0, 255), thick=2) 
            return window_img
     
        windows = list(self.chunks(windows, len(windows) // n_parallel_jobs))
    
        # use joblib to run processing in parallel
        _results = Parallel(n_jobs=n_parallel_jobs)(
            delayed(self.search_windows)(
                image,
                window,
                self.model,
                self.X_scaler,
                self.pca,
                color_space=color_space,
                spatial_size=spatial_size,
                hist_bins=hist_bins,
                orient=orient,
                pix_per_cell=pix_per_cell,
                cell_per_block=cell_per_block,
                hog_channel=hog_channel,
                spatial_feat=spatial_feat,
                hist_feat=hist_feat,
                hog_feat=hog_feat)
            for window in windows)
        
        hot_windows = []
        for hot_window in _results:
            hot_windows.extend(hot_window)
            
        # show objects
        if show_objects:
            window_img = self.draw_boxes(draw_image, hot_windows, color=(0, 0, 255), thick=2)
            return window_img
        
        # # heatmap processing
        heat = np.zeros_like(image[:, :, 0]).astype(np.float)
        box_list = hot_windows
        
        # Add heat to each box in box list
        heat = self.add_heat(heat, box_list)
    
        # Apply threshold to help remove false positives
        heat = self.apply_threshold(heat, 1)
    
        # Visualize the heatmap when displaying    
        heatmap = np.clip(heat, 0, 255)
        
        # show heatmaps
        if show_heatmap:
            return heatmap
    
        # Find final boxes from heatmap using label function
        labels = label(heatmap)
        
        # show contours
        if show_contours:
            window_img = self.draw_labeled_bboxes(np.copy(draw_image), labels)
            return window_img
       
        # # remove false objects and merge multiple objects
        boxes_contours = self.get_bboxes_from_labels(labels)
        boxes_contours = self.remove_outliers(boxes_contours)
        used_boxes = self.update_objects(boxes_contours)
        self.unhide_if_applicable(boxes_contours, used_boxes)
        self.create_new_objects(boxes_contours, used_boxes)
        self.remove_lost_objects()
        
        window_img = self.draw_info(np.copy(draw_image))
        return window_img

