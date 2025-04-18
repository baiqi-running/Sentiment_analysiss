import os
import time
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from tqdm import tqdm
from torch.optim import AdamW, Adam, SGD
from torch.optim.lr_scheduler import CosineAnnealingLR, StepLR, LambdaLR
from transformers import get_linear_schedule_with_warmup
from sklearn.metrics import accuracy_score, f1_score, roc_auc_score
import logging
import matplotlib.pyplot as plt

from ..config.config import Config
from ..utils.utils import setup_logger, save_model, load_model, set_chinese_font

class Trainer:
    def __init__(self, model, train_loader, val_loader, test_loader, noise_test_loader, config):
        """
        初始化训练器
        
        Args:
            model: 模型
            train_loader: 训练数据加载器
            val_loader: 验证数据加载器
            test_loader: 测试数据加载器
            noise_test_loader: 噪声测试数据加载器
            config: 配置对象
        """
        self.model = model
        self.train_loader = train_loader
        self.val_loader = val_loader
        self.test_loader = test_loader
        self.noise_test_loader = noise_test_loader
        self.config = config
        self.device = config.DEVICE
        
        # 设置日志记录器
        self.logger = setup_logger('trainer', os.path.join(config.LOG_DIR, 'training.log'))
        
        # 将模型移至设备
        self.model.to(self.device)
        
        # 设置优化器
        if config.OPTIMIZER == 'adamw':
            self.optimizer = AdamW(
                self.model.parameters(),
                lr=config.LEARNING_RATE,
                weight_decay=config.WEIGHT_DECAY
            )
        elif config.OPTIMIZER == 'adam':
            self.optimizer = Adam(
                self.model.parameters(),
                lr=config.LEARNING_RATE,
                weight_decay=config.WEIGHT_DECAY
            )
        elif config.OPTIMIZER == 'sgd':
            self.optimizer = SGD(
                self.model.parameters(),
                lr=config.LEARNING_RATE,
                momentum=config.MOMENTUM,
                nesterov=config.NESTEROV,
                weight_decay=config.WEIGHT_DECAY
            )
        
        # 计算总训练步数
        total_steps = len(train_loader) * config.NUM_EPOCHS
        warmup_steps = int(total_steps * config.WARMUP_RATIO)
        
        # 设置学习率调度器
        if config.SCHEDULER == 'cosine':
            self.scheduler = CosineAnnealingLR(
                self.optimizer,
                T_max=total_steps,
                eta_min=config.MIN_LR
            )
        elif config.SCHEDULER == 'step':
            self.scheduler = StepLR(
                self.optimizer,
                step_size=config.STEP_SIZE,
                gamma=config.GAMMA
            )
        else:  # linear
            self.scheduler = get_linear_schedule_with_warmup(
                self.optimizer,
                num_warmup_steps=warmup_steps,
                num_training_steps=total_steps
            )
        
        # 计算类别权重
        self.class_weights = self._calculate_class_weights(train_loader)
        self.logger.info(f"类别权重: {self.class_weights}")
        
        # 带权重的损失函数
        if self.class_weights is not None:
            self.criterion = nn.CrossEntropyLoss(
                weight=self.class_weights,
                label_smoothing=config.LABEL_SMOOTHING
            )
            self.logger.info("使用带类别权重的损失函数")
        else:
            self.criterion = nn.CrossEntropyLoss(
                label_smoothing=config.LABEL_SMOOTHING
            )
            self.logger.info("使用标准损失函数（无类别权重）")
        
        # 初始化最佳验证损失和早停计数器
        self.best_val_loss = float('inf')
        self.patience_counter = 0
        
        # 记录训练统计信息
        self.train_losses = []
        self.val_losses = []
        self.train_accuracies = []
        self.val_accuracies = []
        self.alpha_history = []
        
    def _calculate_class_weights(self, train_loader):
        """
        计算类别权重以处理类别不平衡问题
        支持多种计算方法：
        - 'inverse': 权重与样本数量成反比
        - 'inverse_sqrt': 权重与样本数量平方根成反比
        - 'effective_samples': 有效样本数量方法（Cui et al., 2019）
        """
        # 如果不使用类别权重，返回None
        if not self.config.USE_CLASS_WEIGHTS:
            self.logger.info("不使用类别权重")
            return None
            
        # 收集所有标签
        all_labels = []
        for batch in train_loader:
            labels = batch['label']
            all_labels.extend(labels.numpy())
        
        # 统计每个类别的数量
        label_counts = np.bincount(all_labels, minlength=self.config.NUM_CLASSES)
        
        # 记录类别分布
        self.logger.info(f"训练集类别分布: 负面:{label_counts[0]}, 中性:{label_counts[1]}, 正面:{label_counts[2]}")
        
        # 根据选择的方法计算权重
        if self.config.CLASS_WEIGHT_METHOD == 'inverse':
            # 权重与样本数量成反比
            weights = 1.0 / label_counts
            self.logger.info("使用反比类别权重")
        elif self.config.CLASS_WEIGHT_METHOD == 'inverse_sqrt':
            # 权重与样本数量平方根成反比（平滑处理）
            weights = 1.0 / np.sqrt(label_counts)
            self.logger.info("使用反比平方根类别权重")
        elif self.config.CLASS_WEIGHT_METHOD == 'effective_samples':
            # 有效样本数量方法 (Cui et al., Class-Balanced Loss, 2019)
            beta = self.config.EFFECTIVE_NUM_BETA
            weights = (1 - beta) / (1 - beta ** label_counts)
            self.logger.info(f"使用有效样本数量类别权重，beta={beta}")
        else:
            # 默认使用反比权重
            weights = 1.0 / label_counts
            self.logger.info("使用默认反比类别权重")
        
        # 归一化权重，使其和为NUM_CLASSES
        weights = weights / weights.sum() * self.config.NUM_CLASSES
        
        self.logger.info(f"计算得到的类别权重: {weights}")
        
        # 转为张量并移至设备
        weights_tensor = torch.FloatTensor(weights).to(self.device)
        return weights_tensor
    
    def _mixup(self, images, labels, alpha=0.2):
        """Mixup数据增强"""
        if alpha > 0:
            lam = np.random.beta(alpha, alpha)
        else:
            lam = 1
        
        batch_size = images.size(0)
        index = torch.randperm(batch_size).to(images.device)
        
        mixed_images = lam * images + (1 - lam) * images[index, :]
        mixed_labels = lam * labels + (1 - lam) * labels[index]
        
        return mixed_images, mixed_labels
    
    def _cutmix(self, images, labels, alpha=0.2):
        """CutMix数据增强"""
        if alpha > 0:
            lam = np.random.beta(alpha, alpha)
        else:
            lam = 1
        
        batch_size = images.size(0)
        index = torch.randperm(batch_size).to(images.device)
        
        # 生成随机边界框
        bbx1, bby1, bbx2, bby2 = self._rand_bbox(images.size(), lam)
        
        # 应用CutMix
        images[:, :, bbx1:bbx2, bby1:bby2] = images[index, :, bbx1:bbx2, bby1:bby2]
        lam = 1 - ((bbx2 - bbx1) * (bby2 - bby1) / (images.size()[-1] * images.size()[-2]))
        
        mixed_labels = lam * labels + (1 - lam) * labels[index]
        
        return images, mixed_labels
    
    def _rand_bbox(self, size, lam):
        """生成随机边界框"""
        W = size[2]
        H = size[3]
        cut_rat = np.sqrt(1. - lam)
        cut_w = int(W * cut_rat)
        cut_h = int(H * cut_rat)
        
        # uniform
        cx = np.random.randint(W)
        cy = np.random.randint(H)
        
        bbx1 = np.clip(cx - cut_w // 2, 0, W)
        bby1 = np.clip(cy - cut_h // 2, 0, H)
        bbx2 = np.clip(cx + cut_w // 2, 0, W)
        bby2 = np.clip(cy + cut_h // 2, 0, H)
        
        return bbx1, bby1, bbx2, bby2
    
    def train(self):
        """训练模型"""
        self.logger.info(f"开始训练，设备：{self.device}")
        self.logger.info(f"总训练轮次：{self.config.NUM_EPOCHS}")
        self.logger.info(f"总训练批次：{len(self.train_loader)}")
        self.logger.info(f"批次大小：{self.config.BATCH_SIZE}")
        self.logger.info(f"学习率：{self.config.LEARNING_RATE}")
        
        for epoch in range(self.config.NUM_EPOCHS):
            start_time = time.time()
            self.logger.info(f"Epoch {epoch+1}/{self.config.NUM_EPOCHS}")
            
            # 训练一个轮次
            train_loss, train_acc, alpha_values = self._train_epoch()
            
            # 在验证集上评估
            val_loss, val_acc, val_f1 = self._validate()
            
            # 记录统计信息
            self.train_losses.append(train_loss)
            self.val_losses.append(val_loss)
            self.train_accuracies.append(train_acc)
            self.val_accuracies.append(val_acc)
            self.alpha_history.extend(alpha_values)
            
            # 记录学习率
            current_lr = self.scheduler.get_last_lr()[0]
            
            # 计算耗时
            elapsed_time = time.time() - start_time
            
            # 记录训练日志
            self.logger.info(f"训练损失: {train_loss:.4f}, 训练准确率: {train_acc:.4f}")
            self.logger.info(f"验证损失: {val_loss:.4f}, 验证准确率: {val_acc:.4f}, 验证F1: {val_f1:.4f}")
            self.logger.info(f"学习率: {current_lr:.6f}")
            self.logger.info(f"轮次耗时: {elapsed_time:.2f}秒")
            
            # 早停检查
            if val_loss < self.best_val_loss:
                self.best_val_loss = val_loss
                self.patience_counter = 0
                # 保存最佳模型
                save_model(self.model, os.path.join(self.config.MODEL_SAVE_PATH, "best_model.pt"))
                self.logger.info("保存新的最佳模型")
            else:
                self.patience_counter += 1
                self.logger.info(f"验证损失未改善，耐心计数：{self.patience_counter}/{self.config.EARLY_STOP_PATIENCE}")
                
                if self.patience_counter >= self.config.EARLY_STOP_PATIENCE:
                    self.logger.info(f"早停触发，停止训练")
                    break
            
            # 每轮次保存一次模型检查点
            save_model(
                self.model, 
                os.path.join(self.config.MODEL_SAVE_PATH, f"model_epoch_{epoch+1}.pt")
            )
            
            # 保存训练曲线
            self._save_training_curves()
            
            # 分析模态权重分布
            self._analyze_modal_weights(epoch)
        
        # 训练完成后，加载最佳模型
        self.logger.info("加载最佳模型进行测试")
        load_model(self.model, os.path.join(self.config.MODEL_SAVE_PATH, "best_model.pt"))
        
        # 在测试集和噪声测试集上评估
        test_results = self.evaluate(self.test_loader, "测试集")
        noise_test_results = self.evaluate(self.noise_test_loader, "噪声测试集")
        
        # 记录最终结果
        self.logger.info("训练完成，最终结果：")
        self.logger.info(f"测试集 - 准确率: {test_results['accuracy']:.4f}, F1分数: {test_results['f1']:.4f}, AUC: {test_results['auc']:.4f}")
        self.logger.info(f"噪声测试集 - 准确率: {noise_test_results['accuracy']:.4f}, F1分数: {noise_test_results['f1']:.4f}, AUC: {noise_test_results['auc']:.4f}")
        
        return {
            'train_losses': self.train_losses,
            'val_losses': self.val_losses,
            'train_accuracies': self.train_accuracies,
            'val_accuracies': self.val_accuracies,
            'alpha_history': self.alpha_history,
            'test_results': test_results,
            'noise_test_results': noise_test_results
        }
    
    def _train_epoch(self):
        """训练一个轮次"""
        self.model.train()
        total_loss = 0.0
        total_samples = 0
        correct = 0
        alpha_values = []
        
        pbar = tqdm(self.train_loader, desc="训练")
        for batch in pbar:
            # 将数据移至设备
            input_ids = batch['input_ids'].to(self.device)
            attention_mask = batch['attention_mask'].to(self.device)
            images = batch['image'].to(self.device)
            labels = batch['label'].to(self.device)
            
            # 应用数据增强
            if np.random.random() < 0.5:
                images, labels = self._mixup(images, labels, self.config.MIXUP_ALPHA)
            else:
                images, labels = self._cutmix(images, labels, self.config.CUTMIX_ALPHA)
            
            # 清零梯度
            self.optimizer.zero_grad()
            
            # 前向传播
            logits, text_features, image_features, alpha = self.model(input_ids, attention_mask, images)
            
            # 确保标签是长整型
            labels = labels.long()
            
            # 分类损失（已在__init__中添加了类别权重）
            classification_loss = self.criterion(logits, labels)
            
            # 对比损失
            if hasattr(self.model, 'get_contrastive_loss'):
                contrastive_loss = self.model.get_contrastive_loss(
                    text_features, 
                    image_features, 
                    temperature=self.config.TEMPERATURE
                )
                # 加权总损失
                loss = classification_loss + self.config.CONTRASTIVE_WEIGHT * contrastive_loss
            else:
                loss = classification_loss
            
            # 反向传播
            loss.backward()
            
            # 梯度裁剪
            torch.nn.utils.clip_grad_norm_(self.model.parameters(), self.config.GRADIENT_CLIP)
            
            # 更新参数
            self.optimizer.step()
            
            # 更新学习率
            self.scheduler.step()
            
            # 统计指标
            total_loss += loss.item() * labels.size(0)
            total_samples += labels.size(0)
            
            # 计算准确率
            _, predicted = torch.max(logits, 1)
            correct += (predicted == labels).sum().item()
            
            # 记录alpha值（如果有）
            if alpha is not None:
                alpha_values.extend(alpha.detach().cpu().numpy().flatten().tolist())
            
            # 更新进度条
            pbar.set_postfix({
                'loss': loss.item(),
                'acc': correct / total_samples
            })
        
        # 计算平均损失和准确率
        avg_loss = total_loss / total_samples
        accuracy = correct / total_samples
        
        return avg_loss, accuracy, alpha_values
    
    def _validate(self):
        """在验证集上评估模型"""
        self.model.eval()
        total_loss = 0.0
        total_samples = 0
        correct = 0
        all_preds = []
        all_labels = []
        
        with torch.no_grad():
            for batch in tqdm(self.val_loader, desc="验证"):
                # 将数据移至设备
                input_ids = batch['input_ids'].to(self.device)
                attention_mask = batch['attention_mask'].to(self.device)
                images = batch['image'].to(self.device)
                labels = batch['label'].to(self.device)
                
                # 前向传播
                logits, text_features, image_features, _ = self.model(input_ids, attention_mask, images)
                
                # 计算损失
                loss = self.criterion(logits, labels)
                
                # 统计指标
                total_loss += loss.item() * labels.size(0)
                total_samples += labels.size(0)
                
                # 计算准确率
                _, predicted = torch.max(logits, 1)
                correct += (predicted == labels).sum().item()
                
                # 收集预测和标签用于计算F1
                all_preds.extend(predicted.cpu().numpy())
                all_labels.extend(labels.cpu().numpy())
        
        # 计算平均损失和准确率
        avg_loss = total_loss / total_samples
        accuracy = correct / total_samples
        
        # 计算F1分数
        f1 = f1_score(np.array(all_labels), np.array(all_preds), average='macro')
        
        # 统计验证集标签分布
        label_counts = np.bincount(np.array(all_labels))
        self.logger.info(f"验证集标签分布: 负面:{label_counts[0]}, 中性:{label_counts[1]}, 正面:{label_counts[2]}")
        
        return avg_loss, accuracy, f1
    
    def evaluate(self, data_loader, data_name="测试集"):
        """评估模型性能"""
        self.model.eval()
        all_preds = []
        all_labels = []
        all_probs = []
        
        with torch.no_grad():
            for batch in tqdm(data_loader, desc=f"评估{data_name}"):
                # 将数据移至设备
                input_ids = batch['input_ids'].to(self.device)
                attention_mask = batch['attention_mask'].to(self.device)
                images = batch['image'].to(self.device)
                labels = batch['label'].to(self.device)
                
                # 前向传播
                logits, _, _, _ = self.model(input_ids, attention_mask, images)
                
                # 获取预测结果
                probs = F.softmax(logits, dim=1)
                _, predicted = torch.max(logits, 1)
                
                # 收集预测和标签
                all_preds.extend(predicted.cpu().numpy())
                all_labels.extend(labels.cpu().numpy())
                all_probs.extend(probs.cpu().numpy())
        
        # 转换为NumPy数组
        all_preds = np.array(all_preds)
        all_labels = np.array(all_labels)
        all_probs = np.array(all_probs)
        
        # 计算指标
        accuracy = accuracy_score(all_labels, all_preds)
        f1 = f1_score(all_labels, all_preds, average='macro')
        
        # 计算AUC-ROC（多分类）
        auc = 0
        try:
            # 对于多分类，计算每个类别的AUC，然后取平均
            auc = roc_auc_score(
                np.eye(self.config.NUM_CLASSES)[all_labels],  # 转换为one-hot编码
                all_probs,
                multi_class='ovr',
                average='macro'
            )
        except Exception as e:
            self.logger.warning(f"计算AUC时出错: {e}")
        
        # 记录结果
        self.logger.info(f"{data_name} 评估结果:")
        self.logger.info(f"准确率: {accuracy:.4f}")
        self.logger.info(f"F1分数(宏平均): {f1:.4f}")
        self.logger.info(f"AUC-ROC: {auc:.4f}")
        
        return {
            'accuracy': accuracy,
            'f1': f1,
            'auc': auc,
            'predictions': all_preds,
            'labels': all_labels,
            'probabilities': all_probs
        }
    
    def _save_training_curves(self):
        """保存训练和验证曲线"""
        # 设置中文字体
        set_chinese_font()
        
        # 创建图像目录
        plots_dir = os.path.join(self.config.RESULT_DIR, 'plots')
        os.makedirs(plots_dir, exist_ok=True)
        
        # 绘制损失曲线
        plt.figure(figsize=(10, 5))
        plt.plot(self.train_losses, label='训练损失')
        plt.plot(self.val_losses, label='验证损失')
        plt.xlabel('轮次')
        plt.ylabel('损失')
        plt.title('训练和验证损失')
        plt.legend()
        plt.grid(True)
        plt.savefig(os.path.join(plots_dir, 'loss_curve.png'))
        plt.close()
        
        # 绘制准确率曲线
        plt.figure(figsize=(10, 5))
        plt.plot(self.train_accuracies, label='训练准确率')
        plt.plot(self.val_accuracies, label='验证准确率')
        plt.xlabel('轮次')
        plt.ylabel('准确率')
        plt.title('训练和验证准确率')
        plt.legend()
        plt.grid(True)
        plt.savefig(os.path.join(plots_dir, 'accuracy_curve.png'))
        plt.close()
    
    def _analyze_modal_weights(self, epoch):
        """分析模态权重分布"""
        if not self.alpha_history:
            return
        
        # 设置中文字体
        set_chinese_font()
        
        # 创建图像目录
        plots_dir = os.path.join(self.config.RESULT_DIR, 'plots')
        os.makedirs(plots_dir, exist_ok=True)
        
        # 绘制模态权重分布
        plt.figure(figsize=(10, 5))
        plt.hist(self.alpha_history, bins=20, alpha=0.7)
        plt.xlabel('文本权重 (α)')
        plt.ylabel('频率')
        plt.title(f'模态权重分布 (轮次 {epoch+1})')
        plt.grid(True)
        plt.savefig(os.path.join(plots_dir, f'modal_weights_epoch_{epoch+1}.png'))
        plt.close()
        
        # 计算统计信息
        alpha_array = np.array(self.alpha_history)
        mean_alpha = np.mean(alpha_array)
        std_alpha = np.std(alpha_array)
        
        self.logger.info(f"模态权重统计 (轮次 {epoch+1}):")
        self.logger.info(f"平均文本权重 (α): {mean_alpha:.4f}")
        self.logger.info(f"文本权重标准差: {std_alpha:.4f}") 