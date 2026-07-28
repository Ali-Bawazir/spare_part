from django.urls import path

from . import views_preventive

app_name = "preventive"

urlpatterns = [
    # Technician (1 page)
    path("my/", views_preventive.tech_my, name="my"),
    path("my/<int:occurrence_id>/", views_preventive.tech_execute, name="execute"),
    path("my/<int:occurrence_id>/start/", views_preventive.tech_start, name="start"),
    path("my/<int:occurrence_id>/complete/", views_preventive.tech_complete, name="complete"),
    path("my/<int:occurrence_id>/photo/", views_preventive.tech_add_photo, name="add_photo"),
    path("my/<int:occurrence_id>/return/", views_preventive.tech_resume, name="resume"),

    # Manager (6 + 1 detail)
    path("manage/", views_preventive.mgr_dashboard, name="mgr_dashboard"),
    path("manage/today/", views_preventive.mgr_today, name="mgr_today"),
    path("manage/today/regenerate/", views_preventive.mgr_today_regenerate, name="mgr_today_regenerate"),
    path("manage/reviews/", views_preventive.mgr_reviews, name="mgr_reviews"),
    path("manage/reviews/<int:occurrence_id>/approve/", views_preventive.mgr_review_approve, name="review_approve"),
    path("manage/reviews/<int:occurrence_id>/return/", views_preventive.mgr_review_return, name="review_return"),
    path("manage/templates/", views_preventive.mgr_templates, name="mgr_templates"),
    path("manage/templates/new/", views_preventive.mgr_template_create, name="mgr_template_create"),
    path("manage/templates/<int:pk>/", views_preventive.mgr_template_edit, name="mgr_template_edit"),
    path("manage/plans/", views_preventive.mgr_plans, name="mgr_plans"),
    path("manage/plans/new/", views_preventive.mgr_plan_create, name="mgr_plan_create"),
    path("manage/plans/<int:pk>/", views_preventive.mgr_plan_detail, name="mgr_plan_detail"),
    path("manage/plans/<int:pk>/edit/", views_preventive.mgr_plan_edit, name="mgr_plan_edit"),
    path("manage/plans/<int:pk>/assign/", views_preventive.mgr_plan_assign, name="mgr_plan_assign"),
    path("manage/plans/<int:pk>/pause/", views_preventive.mgr_plan_pause, name="mgr_plan_pause"),
    path("manage/plans/<int:pk>/archive/", views_preventive.mgr_plan_archive, name="mgr_plan_archive"),
    path("manage/plans/<int:pk>/run-now/", views_preventive.mgr_plan_run_now, name="mgr_plan_run_now"),
    path("manage/history/", views_preventive.mgr_history, name="mgr_history"),
]