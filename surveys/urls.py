from django.urls import path, include
from . import views

app_name = 'surveys'

urlpatterns = [
    # 🎯 1. API & CÁC TRANG TĨNH CƠ BẢN
    path('api/v1/', include('surveys.api.urls')),
    path('', views.survey_list, name='survey_list'),
    path('document', views.survey_document, name='survey_document'),
    path('send-email-result/', views.send_email_result, name='send_email_result'),
    
    # 🎯 2. CÁC ĐƯỜNG DẪN BÁO CÁO & THỐNG KÊ CỐ ĐỊNH (ĐẶT LÊN TRÊN)
    path('report/', views.reconnect360_demographics_view, name='reconnect_report'),
    path('report/demographics/', views.reconnect360_demographics_view, name='reconnect360_demographics_view'),
    path('report/repeat-users/', views.reconnect360_repeat_users_view, name='report_repeat_users'),
    path('report/cross-analysis/', views.cross_analysis_report_view, name='cross_analysis_report'),
    path('submissions/stats/<int:pk>/', views.submission_stats_detail, name='submission_stats_detail'),
    
    # 🎯 3. BỘ KHẢO SÁT / COMBO CỐ ĐỊNH VÀ DẠNG ĐỘNG
    path('khao-sat-xu-huong-thu-minh-va-muc-do-ket-noi-xa-hoi-o-hoc-sinh-thcs-thpt-1/', views.reconnect360_detail, name='reconnect360_detail'),
    path('combo/<slug:slug>/', views.combo_detail, name='combo_detail'),
    
    # 🎯 4. KHẢO SÁT ĐƠN DÙNG TIỀN TỐ 'detail/' HOẶC 'thank-you/'
    path('thank-you/<slug:slug>', views.survey_thankyou, name='survey_thankyou'),
    path('detail/<slug:slug>/', views.survey_detail, name='survey_detail'),
    path('detail/<slug:slug>/submit/', views.submit_detail, name='submit_detail'),
    
    # 🎯 5. CÁC ĐƯỜNG DẪN DẠNG SLUG ĐỘNG TỔNG QUÁT (ĐẶT Ở CUỐI CÙNG ĐỂ TRÁNH NUỐT ROUTE)
    path('<int:survey_id>/statistics/', views.survey_statistics_view, name='survey_statistics'),
    path('<slug:slug>/submit/', views.submit_survey, name='submit_survey'),
    path('<slug:slug>/result/', views.survey_result_view, name='survey_result'),
]