;;; Generate closed DWG boundaries for Revit from all HATCH objects on one layer.

(defun a101:last-entity (/ entity next)
  (setq entity (entlast))
  (while (and entity (setq next (entnext entity)))
    (setq entity next))
  entity)

(defun a101:new-layer-name (source / base name index)
  (setq base (strcat source "_BOUNDARIES")
        name base
        index 1)
  (while (tblsearch "LAYER" name)
    (setq index (1+ index)
          name (strcat base "_" (itoa index))))
  name)

(defun a101:layer-hatches (layer / all matches index entity)
  (setq all (ssget "_X" '((0 . "HATCH") (410 . "Model")))
        matches (ssadd)
        index 0)
  (if all
    (repeat (sslength all)
      (setq entity (ssname all index)
            index (1+ index))
      (if (= (strcase (cdr (assoc 8 (entget entity)))) (strcase layer))
        (ssadd entity matches))))
  matches)

(defun a101:closed-polyline-p (data / kind flags)
  (setq kind (cdr (assoc 0 data))
        flags (cdr (assoc 70 data)))
  (and (member kind '("LWPOLYLINE" "POLYLINE"))
       flags
       (= 1 (logand flags 1))))

(defun a101:move-boundaries (before target / entity data count)
  (setq entity (entnext before)
        count 0)
  (while entity
    (setq data (entget entity))
    (if (and (a101:closed-polyline-p data)
             (or (= target (cdr (assoc 8 data)))
                 (entmod (subst (cons 8 target) (assoc 8 data) data))))
      (setq count (1+ count)))
    (setq entity (entnext entity)))
  count)

(defun c:A101_HATCH_BOUNDARIES
  (/ *error* layer hatches target previous-layer echo undo before count total
     index generated missing)
  (defun *error* (message)
    (if previous-layer (setvar "CLAYER" previous-layer))
    (if undo (command-s "_.UNDO" "_End"))
    (if echo (setvar "CMDECHO" echo))
    (if message
      (princ (strcat "\nA101: " message "\nUse UNDO to revert this run if needed.")))
    (princ))
  (cond
    ((/= 1 (getvar "TILEMODE"))
     (princ "\nSwitch to the Model tab and run A101_HATCH_BOUNDARIES again."))
    (T
     (setq layer (getstring T "\nHatch layer <1_landscaping>: "))
     (if (= layer "") (setq layer "1_landscaping"))
     (setq hatches (a101:layer-hatches layer)
           total (sslength hatches))
     (if (= 0 total)
       (princ (strcat "\nNo model-space HATCH objects on layer " layer
                      ". If they are in an xref, open its source DWG."))
       (progn
         (setq target (a101:new-layer-name layer)
               echo (getvar "CMDECHO")
               previous-layer (getvar "CLAYER")
               count 0
               missing 0
               index 0)
         (setvar "CMDECHO" 0)
         (command-s "_.UNDO" "_Begin")
         (setq undo T)
         (command-s "_.-LAYER" "_New" target "")
         (setvar "CLAYER" target)
         (repeat total
           (setq before (a101:last-entity))
           (command-s "_.HATCHGENERATEBOUNDARY" (ssname hatches index) "")
           (setq generated (a101:move-boundaries before target)
                 count (+ count generated)
                 index (1+ index))
           (if (= generated 0) (setq missing (1+ missing))))
         (setvar "CLAYER" previous-layer)
         (setq previous-layer nil)
         (command-s "_.UNDO" "_End")
         (setq undo nil)
         (setvar "CMDECHO" echo)
         (setq echo nil)
         (princ (strcat "\nHatches selected: " (itoa total)
                        "; closed boundaries on " target ": " (itoa count) "."))
         (if (> missing 0)
           (princ (strcat "\nHatches without usable boundaries: " (itoa missing)
                          ". See the command history.")))))))
  (princ))

(princ "\nA101_HATCH_BOUNDARIES loaded.")
(princ)
