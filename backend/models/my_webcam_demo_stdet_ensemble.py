# Copyright (c) OpenMMLab. All rights reserved.
"""Webcam Spatio-Temporal Action Detection Demo.

Some codes are based on https://github.com/facebookresearch/SlowFast
"""
import os
import pafy
import datetime
import argparse
import atexit
import copy
import logging
import queue
import threading
import time
from abc import ABCMeta, abstractmethod

import sys
sys.path.append("C:/Users/award/Desktop/workspace/2022-2-SCS4031-SantongSantong/backend/")
import django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
django.setup()

import cv2
import mmcv
import numpy as np
import torch
from mmcv import Config, DictAction
from mmcv.runner import load_checkpoint

from mmaction.models import build_detector

try:
    from mmdet.apis import inference_detector, init_detector
except (ImportError, ModuleNotFoundError):
    raise ImportError(
        "Failed to import `inference_detector` and "
        "`init_detector` form `mmdet.apis`. These apis are "
        "required in this demo! "
    )

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

url = "https://www.youtube.com/watch?v=nTtBxIYrCtU"
video = pafy.new(url)
best = video.getbest(preftype="mp4")


def parse_args():
    parser = argparse.ArgumentParser(
        description="MMAction2 webcam spatio-temporal detection demo"
    )

    parser.add_argument(
        "--config",
        default=("stdet_model/best_models/configs/"),
        help="spatio temporal detection config file path",
    )  # 모델 5개 config 들어있는 폴더
    parser.add_argument(
        "--checkpoint",
        default=("stdet_model/best_models/checkpoints/"),
        help="spatio temporal detection checkpoint file/url",
    )  # 모델 5개 checkpoint 들어있는 폴더
    parser.add_argument(
        "--action-score-thr",
        type=float,
        default=0.92,
        help="the threshold of human action score",
    )  # 앙상블 평균이므로 조금 높게 잡음
    parser.add_argument(
        "--det-config",
        default="stdet_model/my_faster_rcnn_r50_fpn_2x_coco.py",
        help="human detection config file path (from mmdet)",
    )  # object detection 모델 config 경로
    parser.add_argument(
        "--det-checkpoint",
        default=("stdet_model/my_mmdet.pth"),
        help="human detection checkpoint file/url",
    )  # object detection 모델 checkpoint 경로
    parser.add_argument(
        "--det-score-thr",
        type=float,
        default=0.6,
        help="the threshold of human detection score",
    )
    parser.add_argument(
        "--input-video",
        default=best.url,
        type=str,
        help="webcam id or input video file/url",
    )
    parser.add_argument(
        "--label-map", default="stdet_model/label_map.txt", help="label map file"
    )
    parser.add_argument(
        "--device", type=str, default="cuda:0", help="CPU/CUDA device option"
    )
    parser.add_argument(
        "--output-fps", default=15, type=int, help="the fps of demo video output"
    )
    parser.add_argument(
        "--out-filename",
        default="demo/stdet/output.mp4",
        type=str,
        help="the filename of output video",
    )
    parser.add_argument(
        "--show", action="store_true", help="Whether to show results with cv2.imshow"
    )
    parser.add_argument(
        "--display-height",
        type=int,
        default=0,
        help="Image height for human detector and draw frames.",
    )
    parser.add_argument(
        "--display-width",
        type=int,
        default=0,
        help="Image width for human detector and draw frames.",
    )
    parser.add_argument(
        "--predict-stepsize",
        default=5,
        type=int,
        help="give out a prediction per n frames",
    )  # 더 자주 predict 하도록
    parser.add_argument(
        "--clip-vis-length", default=5, type=int, help="Number of draw frames per clip."
    )
    parser.add_argument(
        "--cfg-options",
        nargs="+",
        action=DictAction,
        default={},
        help="override some settings in the used config, the key-value pair "
        "in xxx=yyy format will be merged into config file. For example, "
        "'--cfg-options model.backbone.depth=18 model.backbone.with_cp=True'",
    )

    args = parser.parse_args()
    return args


class TaskInfo:
    """Wapper for a clip.

    Transmit data around three threads.

    1) Read Thread: Create task and put task into read queue. Init `frames`,
        `processed_frames`, `img_shape`, `ratio`, `clip_vis_length`.
    2) Main Thread: Get data from read queue, predict human bboxes and stdet
        action labels, draw predictions and put task into display queue. Init
        `display_bboxes`, `stdet_bboxes` and `action_preds`, update `frames`.
    3) Display Thread: Get data from display queue, show/write frames and
        delete task.
    """

    def __init__(self):
        self.id = -1

        # raw frames, used as human detector input, draw predictions input
        # and output, display input
        self.frames = None

        # stdet params
        self.processed_frames = None  # model inputs
        self.frames_inds = None  # select frames from processed frames
        self.img_shape = None  # model inputs, processed frame shape
        # `action_preds` is `list[list[tuple]]`. The outer brackets indicate
        # different bboxes and the intter brackets indicate different action
        # results for the same bbox. tuple contains `class_name` and `score`.
        self.action_preds = None  # stdet results

        # human bboxes with the format (xmin, ymin, xmax, ymax)
        self.display_bboxes = None  # bboxes coords for self.frames
        self.stdet_bboxes = None  # bboxes coords for self.processed_frames
        self.ratio = None  # processed_frames.shape[1::-1]/frames.shape[1::-1]

        # for each clip, draw predictions on clip_vis_length frames
        self.clip_vis_length = -1

    def add_frames(self, idx, frames, processed_frames):
        """Add the clip and corresponding id.

        Args:
            idx (int): the current index of the clip.
            frames (list[ndarray]): list of images in "BGR" format.
            processed_frames (list[ndarray]): list of resize and normed images
                in "BGR" format.
        """
        self.frames = frames
        self.processed_frames = processed_frames
        self.id = idx
        self.img_shape = processed_frames[0].shape[:2]

    def add_bboxes(self, display_bboxes):
        """Add correspondding bounding boxes."""
        self.display_bboxes = display_bboxes
        self.stdet_bboxes = display_bboxes.clone()
        self.stdet_bboxes[:, ::2] = self.stdet_bboxes[:, ::2] * self.ratio[0]
        self.stdet_bboxes[:, 1::2] = self.stdet_bboxes[:, 1::2] * self.ratio[1]

    def add_action_preds(self, preds):
        """Add the corresponding action predictions."""
        self.action_preds = preds

    def get_model_inputs(self, device):
        """Convert preprocessed images to MMAction2 STDet model inputs."""
        cur_frames = [self.processed_frames[idx] for idx in self.frames_inds]
        input_array = np.stack(cur_frames).transpose((3, 0, 1, 2))[np.newaxis]
        input_tensor = torch.from_numpy(input_array).to(device)
        return dict(
            return_loss=False,
            img=[input_tensor],
            proposals=[[self.stdet_bboxes]],
            img_metas=[[dict(img_shape=self.img_shape)]],
        )


class BaseHumanDetector(metaclass=ABCMeta):
    """Base class for Human Dector.

    Args:
        device (str): CPU/CUDA device option.
    """

    def __init__(self, device):
        self.device = torch.device(device)

    @abstractmethod
    def _do_detect(self, image):
        """Get human bboxes with shape [n, 4].

        The format of bboxes is (xmin, ymin, xmax, ymax) in pixels.
        """

    def predict(self, task):
        """Add keyframe bboxes to task."""
        # keyframe idx == (clip_len * frame_interval) // 2
        keyframe = task.frames[len(task.frames) // 2]

        # call detector
        bboxes = self._do_detect(keyframe)

        # convert bboxes to torch.Tensor and move to target device
        if isinstance(bboxes, np.ndarray):
            bboxes = torch.from_numpy(bboxes).to(self.device)
        elif isinstance(bboxes, torch.Tensor) and bboxes.device != self.device:
            bboxes = bboxes.to(self.device)

        # update task
        task.add_bboxes(bboxes)

        return task


class MmdetHumanDetector(BaseHumanDetector):
    """Wrapper for mmdetection human detector.

    Args:
        config (str): Path to mmdetection config.
        ckpt (str): Path to mmdetection checkpoint.
        device (str): CPU/CUDA device option.
        score_thr (float): The threshold of human detection score.
        person_classid (int): Choose class from detection results.
            Default: 0. Suitable for COCO pretrained models.
    """

    def __init__(self, config, ckpt, device, score_thr, person_classid=0):
        super().__init__(device)
        self.model = init_detector(config, ckpt, device)
        self.person_classid = person_classid
        self.score_thr = score_thr

    def _do_detect(self, image):
        """Get bboxes in shape [n, 4] and values in pixels."""
        result = inference_detector(self.model, image)[self.person_classid]
        result = result[result[:, 4] >= self.score_thr][:, :4]
        return result


class StdetPredictor:
    """Wrapper for MMAction2 spatio-temporal action models.

    Args:
        config (str): Path to stdet config.
        ckpt (str): Path to stdet checkpoint.
        device (str): CPU/CUDA device option.
        score_thr (float): The threshold of human action score.
        label_map_path (str): Path to label map file. The format for each line
            is `{class_id}: {class_name}`.
    """

    def __init__(self, config, checkpoint, device, score_thr, label_map_path):
        self.score_thr = score_thr

        # load model
        config.model.backbone.pretrained = None
        model = build_detector(config.model, test_cfg=config.get("test_cfg"))
        load_checkpoint(model, checkpoint, map_location="cpu")
        model.to(device)
        model.eval()
        self.model = model
        self.device = device

        # init label map, aka class_id to class_name dict
        with open(label_map_path) as f:
            lines = f.readlines()
        lines = [x.strip().split(": ") for x in lines]
        self.label_map = {int(x[0]): x[1] for x in lines}
        try:
            if config["data"]["train"]["custom_classes"] is not None:
                self.label_map = {
                    id + 1: self.label_map[cls]
                    for id, cls in enumerate(config["data"]["train"]["custom_classes"])
                }
        except KeyError:
            pass

    def predict(self, task):
        """Spatio-temporval Action Detection model inference."""
        # No need to do inference if no one in keyframe
        if len(task.stdet_bboxes) == 0:
            return task

        with torch.no_grad():
            result = self.model(**task.get_model_inputs(self.device))[0]

        # pack results of human detector and stdet
        preds = []
        for _ in range(task.stdet_bboxes.shape[0]):
            preds.append([])
        for class_id in range(len(result)):
            if class_id + 1 not in self.label_map:
                continue
            for bbox_id in range(task.stdet_bboxes.shape[0]):
                if result[class_id][bbox_id, 4] > self.score_thr:
                    preds[bbox_id].append(
                        (self.label_map[class_id + 1], result[class_id][bbox_id, 4])
                    )

        # update task
        # `preds` is `list[list[tuple]]`. The outer brackets indicate
        # different bboxes and the intter brackets indicate different action
        # results for the same bbox. tuple contains `class_name` and `score`.
        task.add_action_preds(preds)

        return task


class ClipHelper:
    """Multithrading utils to manage the lifecycle of task."""

    def __init__(
        self,
        config,
        display_height=0,
        display_width=0,
        input_video=best.url,
        predict_stepsize=40,
        output_fps=25,
        clip_vis_length=8,
        out_filename="demo/output.mp4",
        show=True,
        stdet_input_shortside=256,
    ):
        self.cnt = 0
        # stdet sampling strategy
        val_pipeline = config.data.val.pipeline
        sampler = [x for x in val_pipeline if x["type"] == "SampleAVAFrames"][0]
        clip_len, frame_interval = sampler["clip_len"], sampler["frame_interval"]
        self.window_size = clip_len * frame_interval

        # asserts
        assert out_filename or show, "out_filename and show cannot both be None"
        assert clip_len % 2 == 0, "We would like to have an even clip_len"
        assert clip_vis_length <= predict_stepsize
        assert 0 < predict_stepsize <= self.window_size

        # source params

        self.cap = cv2.VideoCapture(input_video)
        self.webcam = False
        assert self.cap.isOpened()

        # stdet input preprocessing params
        h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.stdet_input_size = mmcv.rescale_size(
            (w, h), (stdet_input_shortside, np.Inf)
        )
        img_norm_cfg = config["img_norm_cfg"]
        if "to_rgb" not in img_norm_cfg and "to_bgr" in img_norm_cfg:
            to_bgr = img_norm_cfg.pop("to_bgr")
            img_norm_cfg["to_rgb"] = to_bgr
        img_norm_cfg["mean"] = np.array(img_norm_cfg["mean"])
        img_norm_cfg["std"] = np.array(img_norm_cfg["std"])
        self.img_norm_cfg = img_norm_cfg

        # task init params
        self.clip_vis_length = clip_vis_length
        self.predict_stepsize = predict_stepsize
        self.buffer_size = self.window_size - self.predict_stepsize
        frame_start = self.window_size // 2 - (clip_len // 2) * frame_interval
        self.frames_inds = [frame_start + frame_interval * i for i in range(clip_len)]
        self.buffer = []
        self.processed_buffer = []

        # output/display params
        if display_height > 0 and display_width > 0:
            self.display_size = (display_width, display_height)
        elif display_height > 0 or display_width > 0:
            self.display_size = mmcv.rescale_size(
                (w, h), (np.Inf, max(display_height, display_width))
            )
        else:
            self.display_size = (w, h)
        self.ratio = tuple(
            n / o for n, o in zip(self.stdet_input_size, self.display_size)
        )
        if output_fps <= 0:
            self.output_fps = int(self.cap.get(cv2.CAP_PROP_FPS))
        else:
            self.output_fps = output_fps
        self.show = show
        self.video_writer = None
        if out_filename is not None:
            self.video_writer = self.get_output_video_writer(out_filename)
        display_start_idx = self.window_size // 2 - self.predict_stepsize // 2
        self.display_inds = [
            display_start_idx + i for i in range(self.predict_stepsize)
        ]

        # display multi-theading params
        self.display_id = -1  # task.id for display queue
        self.display_queue = {}
        self.display_lock = threading.Lock()
        self.output_lock = threading.Lock()

        # read multi-theading params
        self.read_id = -1  # task.id for read queue
        self.read_id_lock = threading.Lock()
        self.read_queue = queue.Queue()
        self.read_lock = threading.Lock()
        self.not_end = True  # cap.read() flag

        # program state
        self.stopped = False

        atexit.register(self.clean)

    def read_fn(self):
        """Main function for read thread.

        Contains three steps:

        1) Read and preprocess (resize + norm) frames from source.
        2) Create task by frames from previous step and buffer.
        3) Put task into read queue.
        """
        was_read = True
        start_time = time.time()
        while was_read and not self.stopped:
            # init task
            task = TaskInfo()
            task.clip_vis_length = self.clip_vis_length
            task.frames_inds = self.frames_inds
            task.ratio = self.ratio

            # read buffer
            frames = []
            processed_frames = []
            if len(self.buffer) != 0:
                frames = self.buffer
            if len(self.processed_buffer) != 0:
                processed_frames = self.processed_buffer

            # read and preprocess frames from source and update task
            with self.read_lock:
                before_read = time.time()
                read_frame_cnt = self.window_size - len(frames)
                while was_read and len(frames) < self.window_size:
                    was_read, frame = self.cap.read()
                    if not self.webcam:
                        # Reading frames too fast may lead to unexpected
                        # performance degradation. If you have enough
                        # resource, this line could be commented.
                        time.sleep(1 / self.output_fps)
                    if was_read:
                        frames.append(mmcv.imresize(frame, self.display_size))
                        processed_frame = mmcv.imresize(
                            frame, self.stdet_input_size
                        ).astype(np.float32)
                        _ = mmcv.imnormalize_(processed_frame, **self.img_norm_cfg)
                        processed_frames.append(processed_frame)
            task.add_frames(self.read_id + 1, frames, processed_frames)

            # update buffer
            if was_read:
                self.buffer = frames[-self.buffer_size :]
                self.processed_buffer = processed_frames[-self.buffer_size :]

            # update read state
            with self.read_id_lock:
                self.read_id += 1
                self.not_end = was_read

            self.read_queue.put((was_read, copy.deepcopy(task)))
            cur_time = time.time()
            logger.debug(
                f"Read thread: {1000*(cur_time - start_time):.0f} ms, "
                f"{read_frame_cnt / (cur_time - before_read):.0f} fps"
            )
            start_time = cur_time

    def display_fn(self):
        """Main function for display thread.

        Read data from display queue and display predictions.
        """
        start_time = time.time()
        while not self.stopped:
            # get the state of the read thread
            with self.read_id_lock:
                read_id = self.read_id
                not_end = self.not_end

            with self.display_lock:
                # If video ended and we have display all frames.
                if not not_end and self.display_id == read_id:
                    break

                # If the next task are not available, wait.
                if (
                    len(self.display_queue) == 0
                    or self.display_queue.get(self.display_id + 1) is None
                ):
                    time.sleep(0.02)
                    continue

                # get display data and update state
                self.display_id += 1
                was_read, task = self.display_queue[self.display_id]
                del self.display_queue[self.display_id]
                display_id = self.display_id

            # do display predictions
            with self.output_lock:
                if was_read and task.id == 0:
                    # the first task
                    cur_display_inds = range(self.display_inds[-1] + 1)
                elif not was_read:
                    # the last task
                    cur_display_inds = range(self.display_inds[0], len(task.frames))
                else:
                    cur_display_inds = self.display_inds

                for frame_id in cur_display_inds:
                    frame = task.frames[frame_id]
                    if self.show:
                        cv2.imshow("Demo", frame)
                        cv2.waitKey(int(1000 / self.output_fps))
                    if self.video_writer:
                        self.video_writer.write(frame)

            cur_time = time.time()
            logger.debug(
                f"Display thread: {1000*(cur_time - start_time):.0f} ms, "
                f"read id {read_id}, display id {display_id}"
            )
            start_time = cur_time

    def __iter__(self):
        return self

    def __next__(self):
        """Get data from read queue.

        This function is part of the main thread.
        """
        if self.read_queue.qsize() == 0:
            time.sleep(0.02)
            return not self.stopped, None

        was_read, task = self.read_queue.get()
        if not was_read:
            # If we reach the end of the video, there aren't enough frames
            # in the task.processed_frames, so no need to model inference
            # and draw predictions. Put task into display queue.
            with self.read_id_lock:
                read_id = self.read_id
            with self.display_lock:
                self.display_queue[read_id] = was_read, copy.deepcopy(task)

            # main thread doesn't need to handle this task again
            task = None
        return was_read, task

    def start(self):
        """Start read thread and display thread."""
        self.read_thread = threading.Thread(
            target=self.read_fn, args=(), name="VidRead-Thread", daemon=True
        )
        self.read_thread.start()
        self.display_thread = threading.Thread(
            target=self.display_fn, args=(), name="VidDisplay-Thread", daemon=True
        )
        self.display_thread.start()

        return self

    def clean(self):
        """Close all threads and release all resources."""
        self.stopped = True
        self.read_lock.acquire()
        self.cap.release()
        self.read_lock.release()
        self.output_lock.acquire()
        cv2.destroyAllWindows()
        if self.video_writer:
            self.video_writer.release()
        self.output_lock.release()

    def join(self):
        """Waiting for the finalization of read and display thread."""
        self.read_thread.join()
        self.display_thread.join()

    def display(self, task):
        """Add the visualized task to the display queue.

        Args:
            task (TaskInfo object): task object that contain the necessary
            information for prediction visualization.
        """
        with self.display_lock:
            self.display_queue[task.id] = (True, task)

    def detect(self, task):
        if task.action_preds is not None:
            for i in task.action_preds:
                if len(i) != 0:
                    self.cnt += 1

    def detect_drowning(self, task):
        # now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.detect(task)
        #if self.cnt != 0 and self.cnt % 9 == 0:
        if self.cnt != 0 and self.cnt == 9:  # 딱 한 번만 캡처되게 저장
            # cv2.imwrite("./static/"+str(now)+".jpg", task.frames[self.display_inds[0]])
            cv2.imwrite("./static/drowning.jpg", task.frames[self.display_inds[0]])
            # self.cnt = 0

    def get_output_video_writer(self, path):
        """Return a video writer object.

        Args:
            path (str): path to the output video file.
        """
        return cv2.VideoWriter(
            filename=path,
            fourcc=cv2.VideoWriter_fourcc(*"mp4v"),
            fps=float(self.output_fps),
            frameSize=self.display_size,
            isColor=True,
        )


class BaseVisualizer(metaclass=ABCMeta):
    """Base class for visualization tools."""

    def __init__(self, max_labels_per_bbox):
        self.max_labels_per_bbox = max_labels_per_bbox

    def draw_predictions(self, task):
        """Visualize stdet predictions on raw frames."""
        # read bboxes from task
        bboxes = task.display_bboxes.cpu().numpy()

        # draw predictions and update task
        keyframe_idx = len(task.frames) // 2
        draw_range = [
            keyframe_idx - task.clip_vis_length // 2,
            keyframe_idx + (task.clip_vis_length - 1) // 2,
        ]
        assert draw_range[0] >= 0 and draw_range[1] < len(task.frames)
        task.frames = self.draw_clip_range(
            task.frames, task.action_preds, bboxes, draw_range
        )

        return task

    def draw_clip_range(self, frames, preds, bboxes, draw_range):
        """Draw a range of frames with the same bboxes and predictions."""
        # no predictions to be draw
        if bboxes is None or len(bboxes) == 0:
            return frames

        # draw frames in `draw_range`
        left_frames = frames[: draw_range[0]]
        right_frames = frames[draw_range[1] + 1 :]
        draw_frames = frames[draw_range[0] : draw_range[1] + 1]

        # get labels(texts) and draw predictions
        draw_frames = [
            self.draw_one_image(frame, bboxes, preds) for frame in draw_frames
        ]

        return list(left_frames) + draw_frames + list(right_frames)

    @abstractmethod
    def draw_one_image(self, frame, bboxes, preds):
        """Draw bboxes and corresponding texts on one frame."""

    @staticmethod
    def abbrev(name):
        """Get the abbreviation of label name:

        'take (an object) from (a person)' -> 'take ... from ...'
        """
        while name.find("(") != -1:
            st, ed = name.find("("), name.find(")")
            name = name[:st] + "..." + name[ed + 1 :]
        return name


class DefaultVisualizer(BaseVisualizer):
    """Tools to visualize predictions.

    Args:
        max_labels_per_bbox (int): Max number of labels to visualize for a
            person box. Default: 5.
        plate (str): The color plate used for visualization. Two recommended
            plates are blue plate `03045e-023e8a-0077b6-0096c7-00b4d8-48cae4`
            and green plate `004b23-006400-007200-008000-38b000-70e000`. These
            plates are generated by https://coolors.co/.
            Default: '03045e-023e8a-0077b6-0096c7-00b4d8-48cae4'.
        text_fontface (int): Fontface from OpenCV for texts.
            Default: cv2.FONT_HERSHEY_DUPLEX.
        text_fontscale (float): Fontscale from OpenCV for texts.
            Default: 0.5.
        text_fontcolor (tuple): fontface from OpenCV for texts.
            Default: (255, 255, 255).
        text_thickness (int): Thickness from OpenCV for texts.
            Default: 1.
        text_linetype (int): LInetype from OpenCV for texts.
            Default: 1.
    """

    def __init__(
        self,
        max_labels_per_bbox=5,
        plate="03045e-023e8a-0077b6-0096c7-00b4d8-48cae4",
        text_fontface=cv2.FONT_HERSHEY_DUPLEX,
        text_fontscale=0.5,
        text_fontcolor=(255, 255, 255),  # white
        text_thickness=1,
        text_linetype=1,
    ):
        super().__init__(max_labels_per_bbox=max_labels_per_bbox)
        self.text_fontface = text_fontface
        self.text_fontscale = text_fontscale
        self.text_fontcolor = text_fontcolor
        self.text_thickness = text_thickness
        self.text_linetype = text_linetype

        def hex2color(h):
            """Convert the 6-digit hex string to tuple of 3 int value (RGB)"""
            return (int(h[:2], 16), int(h[2:4], 16), int(h[4:], 16))

        plate = plate.split("-")
        self.plate = [hex2color(h) for h in plate]

    def draw_one_image(self, frame, bboxes, preds):
        """Draw predictions on one image."""
        for bbox, pred in zip(bboxes, preds):
            # draw bbox
            box = bbox.astype(np.int64)
            st, ed = tuple(box[:2]), tuple(box[2:])
            cv2.rectangle(frame, st, ed, (0, 0, 255), 2)

            # draw texts
            for k, (label, score) in enumerate(pred):
                if k >= self.max_labels_per_bbox:
                    break
                text = f"{self.abbrev(label)}: {score:.4f}"
                location = (0 + st[0], 18 + k * 18 + st[1])
                textsize = cv2.getTextSize(
                    text, self.text_fontface, self.text_fontscale, self.text_thickness
                )[0]
                textwidth = textsize[0]
                diag0 = (location[0] + textwidth, location[1] - 14)
                diag1 = (location[0], location[1] + 2)
                cv2.rectangle(frame, diag0, diag1, self.plate[k + 1], -1)
                cv2.putText(
                    frame,
                    text,
                    location,
                    self.text_fontface,
                    self.text_fontscale,
                    self.text_fontcolor,
                    self.text_thickness,
                    self.text_linetype,
                )

        return frame


def build_model(args, idx, config, ckpts):
    # config=configs[idx-1]  ## config가 배열일 때(모델마다 각각 config 만들어줄 때)
    stdet_predictor = StdetPredictor(
        config=config,
        checkpoint=os.path.join(args.checkpoints, ckpts[idx-1]),
        device=args.device,
        score_thr=args.action_score_thr,
        label_map_path=args.label_map)

    # return config, stdet_predictor
    return stdet_predictor


def main(args):
    # init human detector
    human_detector = MmdetHumanDetector(
        args.det_config, args.det_checkpoint, args.device, args.det_score_thr
    )

    # init action detector
    # config = Config.fromfile(args.config)
    # config.merge_from_dict(args.cfg_options)
    configs = [file for file in os.listdir(args.configs)]
    checkpoints = [file for file in os.listdir(args.checkpoints)]
    config = Config.fromfile(os.path.join(args.configs, configs[0]))
    config.merge_from_dict(args.cfg_options)

    stdet_predictor1 = build_model(args, 1, config, checkpoints)
    stdet_predictor2 = build_model(args, 2, config, checkpoints)
    stdet_predictor3 = build_model(args, 3, config, checkpoints)
    stdet_predictor4 = build_model(args, 4, config, checkpoints)
    stdet_predictor5 = build_model(args, 5, config, checkpoints)

    try:
        # In our spatiotemporal detection demo, different actions should have
        # the same number of bboxes.
        config["model"]["test_cfg"]["rcnn"]["action_thr"] = 0.0
    except KeyError:
        pass

    # init clip helper
    clip_helper = ClipHelper(
        config=config,
        display_height=args.display_height,
        display_width=args.display_width,
        input_video=args.input_video,
        predict_stepsize=args.predict_stepsize,
        output_fps=args.output_fps,
        clip_vis_length=args.clip_vis_length,
        out_filename=args.out_filename,
        show=args.show,
    )

    # init visualizer
    vis = DefaultVisualizer()

    # start read and display thread
    clip_helper.start()

    try:
        # Main thread main function contains:
        # 1) get data from read queue
        # 2) get human bboxes and stdet predictions
        # 3) draw stdet predictions and update task
        # 4) put task into display queue
        for able_to_read, task in clip_helper:
            # get data from read queue

            if not able_to_read:
                # read thread is dead and all tasks are processed
                break

            if task is None:
                # when no data in read queue, wait
                time.sleep(0.01)
                continue

            inference_start = time.time()

            # get human bboxes
            human_detector.predict(task)  # [[사람1 bbox], [사람2 bbox], ...]

            # get stdet predictions
            # stdet_predictor.predict(task)  # 모델 하나일 때
            task1 = stdet_predictor1.predict(task)  # task1.action_preds = [ [사람1에 대해서 (액션, 스코어), (액션, 스코어)], [사람2에 대해서 (액션, 스코어)], ... ]
            task2 = stdet_predictor2.predict(task)  # task2.action_preds = [ [사람1에 대해서 (액션, 스코어), (액션, 스코어)], [사람2에 대해서 (액션, 스코어)], ... ]
            task3 = stdet_predictor3.predict(task)  # task3.action_preds = [ [사람1에 대해서 (액션, 스코어), (액션, 스코어)], [사람2에 대해서 (액션, 스코어)], ... ]
            task4 = stdet_predictor4.predict(task)  # task4.action_preds = [ [사람1에 대해서 (액션, 스코어), (액션, 스코어)], [사람2에 대해서 (액션, 스코어)], ... ]
            task5 = stdet_predictor5.predict(task)  # task5.action_preds = [ [사람1에 대해서 (액션, 스코어), (액션, 스코어)], [사람2에 대해서 (액션, 스코어)], ... ]
            
            # 각 모델 결과 voting -> task.action_preds 업데이트
            preds = [list() for _ in task.stdet_bboxes]  # 사람 객체만큼의 빈 리스트로 이루어진 리스트 [[], [], ...] 
            for idx, bbox in enumerate(preds):
                result = {'drowning': 0, 'swimming': 0}
                # result = {'drowning': 0}
                preds[idx] = task1.action_preds[idx] + task2.action_preds[idx] + task3.action_preds[idx] + task4.action_preds[idx] + task5.action_preds[idx]
                    # ex. [[('swimming', 0.988), ('drowning', 0.38), ('swimming', 0.83), ('drowning', 0.56), ('swimming', 0.967)], 
                    #      [('swimming', 0.988), ('swimming', 0.988), ('swimming', 0.998), ('drowning', 0.23)], ...]
                for tup in preds[idx]:
                    result[tup[0]] += tup[1]
                result['drowning'] /= 5
                result['swimming'] /= 5
                    # preds : [[('drowning', 0.94), ('swimming', 2.785)], [('drowning', 0.23), ('swimming', 2.974)], ...]
                del result['swimming']
                if result['drowning'] < args.action_score_thr:
                    del result['drowning']
                result = list(result.items())     
                preds[idx] = result       
            preds = [pred for pred in preds if len(pred)>0]  # 행동 탐지 안 된 사람은 박스 그리지 않도록
            task.action_preds = preds  # task.add_action_preds(preds)

            # draw stdet predictions in raw frames
            vis.draw_predictions(task)
            logger.info(f"Stdet Results: {task.action_preds}")

            # add draw frames to display queue
            clip_helper.display(task)

            # detect drawning frame
            clip_helper.detect_drowning(task)

            logger.debug(
                "Main thread inference time "
                f"{1000*(time.time() - inference_start):.0f} ms"
            )

        # wait for display thread
        clip_helper.join()
    except KeyboardInterrupt:
        pass
    finally:
        # close read & display thread, release all resources
        clip_helper.clean()


if __name__ == "__main__":
    main(parse_args())
